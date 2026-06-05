"""
core/ocio_manager.py

Thin wrapper around PyOpenColorIO for BlastPlayer's GPU colour-management pipeline.
Degrades gracefully when the library is not installed.

PyOpenColorIO is imported LAZILY (inside methods only) to avoid a DLL conflict
with PyOpenGL when both are loaded in the same process at startup.

Usage
-----
    from core.ocio_manager import OCIOManager, OCIO_AVAILABLE

    mgr = OCIOManager()
    mgr.load_config("/path/to/config.ocio")
    mgr.set_transform("ACES - ACEScg", "ACES", "sRGB")
    src, luts = mgr.build_gpu_shader()
    # pass src + luts to VideoCanvas.set_ocio()
"""

import importlib.util
import array as _array

# Check availability without importing (avoids DLL conflict with PyOpenGL at startup)
OCIO_AVAILABLE = importlib.util.find_spec("PyOpenColorIO") is not None

from . import constants


def _ocio():
    """Lazy import of PyOpenColorIO — called only when OCIO is actually used."""
    import PyOpenColorIO as _OCIO
    return _OCIO


class OCIOManager:
    """
    Manages an OCIO config and builds the GPU colour-transform shader for VideoCanvas.

    LUT list items returned by build_gpu_shader():
        (sampler_name: str, width: int, height: int, data: bytes, is_3d: bool)
    """

    def __init__(self):
        self._config  = None
        self._src_cs  = ""
        self._display = ""
        self._view    = ""

    # ── Config loading ───────────────────────────────────────────────── #

    def load_config(self, path: str = "") -> bool:
        """
        Load an OCIO config from *path*.
        If path is empty, fall back to constants.OCIO_CONFIG_PATH, then the
        $OCIO environment variable, then PyOpenColorIO's built-in default.
        Returns True on success.
        """
        if not OCIO_AVAILABLE:
            return False
        OCIO = _ocio()
        target = path or constants.OCIO_CONFIG_PATH
        try:
            if target:
                self._config = OCIO.Config.CreateFromFile(target)
            else:
                self._config = OCIO.GetCurrentConfig()
            displays      = self.get_displays()
            self._display = displays[0] if displays else ""
            views         = self.get_views(self._display)
            self._view    = views[0] if views else ""
            css           = self.get_input_color_spaces()
            self._src_cs  = css[0] if css else ""
            return True
        except Exception as exc:
            print(f"[OCIO] load_config failed: {exc}")
            self._config = None
            return False

    @property
    def is_ready(self) -> bool:
        return OCIO_AVAILABLE and self._config is not None

    # ── Transform selection ──────────────────────────────────────────── #

    def set_transform(self, src_cs: str, display: str, view: str):
        self._src_cs  = src_cs
        self._display = display
        self._view    = view

    # ── Introspection ────────────────────────────────────────────────── #

    def get_input_color_spaces(self) -> list:
        if not self.is_ready:
            return []
        try:
            return [self._config.getColorSpaceNameByIndex(i)
                    for i in range(self._config.getNumColorSpaces())]
        except Exception:
            return []

    def get_displays(self) -> list:
        if not self.is_ready:
            return []
        try:
            return list(self._config.getDisplays())
        except Exception:
            return []

    def get_views(self, display: str) -> list:
        if not self.is_ready:
            return []
        try:
            return list(self._config.getViews(display))
        except Exception:
            return []

    def current_src_cs(self) -> str:  return self._src_cs
    def current_display(self) -> str: return self._display
    def current_view(self) -> str:    return self._view

    # ── GPU shader build ─────────────────────────────────────────────── #

    def build_gpu_shader(self) -> tuple:
        """
        Build the OCIO GPU shader for the current transform.

        Returns (glsl_func_source: str, lut_list: list) where each lut_list
        item is (sampler_name, width, height, data_bytes, is_3d).

        Returns ("", []) on failure.
        """
        if not self.is_ready or not self._src_cs or not self._display or not self._view:
            return ("", [])
        OCIO = _ocio()
        try:
            processor = self._config.getProcessor(
                self._src_cs,
                OCIO.DisplayViewTransform(
                    src=self._src_cs,
                    display=self._display,
                    view=self._view,
                ),
            )
        except Exception:
            try:
                dst_cs    = self._config.getDisplayViewColorSpaceName(
                    self._display, self._view)
                processor = self._config.getProcessor(self._src_cs, dst_cs)
            except Exception as exc:
                print(f"[OCIO] getProcessor failed: {exc}")
                return ("", [])

        try:
            gpu_proc = processor.getDefaultGPUProcessor()
        except Exception as exc:
            print(f"[OCIO] getDefaultGPUProcessor failed: {exc}")
            return ("", [])

        try:
            shader_desc = OCIO.GpuShaderDesc.CreateShaderDesc(
                language=OCIO.GPU_LANGUAGE_GLSL_1_3,
                functionName="OCIODisplay",
                resourcePrefix="ocio_",
            )
            gpu_proc.extractGpuShaderInfo(shader_desc)
        except Exception as exc:
            print(f"[OCIO] extractGpuShaderInfo failed: {exc}")
            return ("", [])

        func_src = shader_desc.getShaderText()
        luts     = self._extract_luts(shader_desc)
        return (func_src, luts)

    # ── Private helpers ──────────────────────────────────────────────── #

    @staticmethod
    def _extract_luts(shader_desc) -> list:
        luts = []
        try:
            n_tex = shader_desc.getNumTextures()
        except Exception:
            n_tex = 0

        for i in range(n_tex):
            try:
                name, sampler, w, h, channel_order, interp = \
                    shader_desc.getTexture(i)
                values = shader_desc.getTextureValues(i)
                data   = _array.array('f', values).tobytes()
                luts.append((sampler, w, h, data, False))
            except Exception:
                pass

        try:
            n_3d = shader_desc.getNum3DTextures()
        except Exception:
            n_3d = 0

        for i in range(n_3d):
            try:
                name, sampler, edge_len, interp = shader_desc.get3DTexture(i)
                values = shader_desc.get3DTextureValues(i)
                data   = _array.array('f', values).tobytes()
                luts.append((sampler, edge_len, edge_len, data, True))
            except Exception:
                pass

        return luts
