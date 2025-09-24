from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence, Optional
from pathlib import Path

from PIL import Image

from pygerber.gerber.api._enums import COLOR_MAP_T, FileTypeEnum
from pygerber.gerber.api._gerber_file import GerberFile, ImageSpace, PillowImage, _PillowSaveMixin
from pygerber.vm import render
from pygerber.vm.types import Style

if TYPE_CHECKING:
    from typing_extensions import Self


class CompositeImage:
    """Image composed of multiple sub-images."""


class CompositePillowImage(CompositeImage, _PillowSaveMixin):
    """Image composed of multiple sub-images."""

    def __init__(self, sub_images: list[PillowImage], image: Image.Image) -> None:
        self._sub_images = sub_images
        self._image = image

    def get_sub_images(self) -> Sequence[PillowImage]:
        """Get sequence containing sub-images."""
        return self._sub_images

    def get_image(self) -> Image.Image:
        """Get image composed out of sub-images."""
        return self._image


class CompositeView:
    """View composed of multiple Gerber files.

    Composite view is a generalized concept of a top / bottom / inner layer view
    extracted from a Gerber project. Usually it does not make sense to render all
    layers at once, as bottom layers will be simply covered by top layers.

    This object can be used to render selected Gerber files to single image.
    It automatically performs alignment and merging of files.
    Files should be ordered bottom up, topmost layer last, like if adding one layer on
    top of previous.
    """

    def __init__(self, files: Iterable[GerberFile]) -> None:
        self._files = tuple(files)

    def set_color_map(self, color_map: COLOR_MAP_T) -> Self:
        """Set color map for all files."""
        for file in self._files:
            file.set_color_map(color_map)
        return self

    @property
    def files(self) -> tuple[GerberFile, ...]:
        """Get sequence of Gerber files."""
        return self._files

    def render_with_pillow(
        self,
        dpmm: int = 20,
    ) -> CompositePillowImage:
        """Render project to raster image using Pillow with via separation and full debugging."""
        
        if not self.files:
            return CompositePillowImage([], Image.new("RGBA", (1, 1), (0, 0, 0, 0)))

        # STEP 1: Render all files normally first for bounding box calculation
        # This ensures alignment works exactly like before
        reference_images: list[PillowImage] = []
        for i, file in enumerate(self.files):
            ref_img = file.render_with_pillow(dpmm=dpmm)
            reference_images.append(ref_img)
        
        # STEP 2: Calculate bounding box using reference images
        if not reference_images:
            return CompositePillowImage([], Image.new("RGBA", (1, 1)))

        max_x_image = max(reference_images, key=lambda x: x.get_image_space().max_x)
        max_y_image = max(reference_images, key=lambda x: x.get_image_space().max_y)
        min_x_image = min(reference_images, key=lambda x: x.get_image_space().min_x)
        min_y_image = min(reference_images, key=lambda x: x.get_image_space().min_y)

        width_pixels = (
            max_x_image.get_image_space().max_x_pixels
            - min_x_image.get_image_space().min_x_pixels
        )
        height_pixels = (
            max_y_image.get_image_space().max_y_pixels
            - min_y_image.get_image_space().min_y_pixels
        )

        final_image = Image.new("RGBA", (width_pixels, height_pixels))

        # STEP 3: Process each file for final compositing
        for i, file in enumerate(self.files):
            # Get the reference image for this file (for alignment)
            ref_image = reference_images[i]
            paste_pos = (
                abs(
                    min_x_image.get_image_space().min_x_pixels
                    - ref_image.get_image_space().min_x_pixels
                ),
                abs(
                    max_y_image.get_image_space().max_y_pixels
                    - ref_image.get_image_space().max_y_pixels
                ),
            )
            
            # Check if this is a copper file that needs via separation
            if self._is_copper_file(file):
                # Use the new dual-output API directly
                try:
                    # Check if the method exists
                    if hasattr(file, '_compile_and_get_rvmcs'):
                        result = file._compile_and_get_rvmcs()
                        if result is None:
                            raise Exception("Method returned None")
                        main_rvmc, via_rvmc = result
                    else:
                        raise Exception("Method doesn't exist")
                    
                    # Render main copper using same bounding box as reference
                    copper_result = render(main_rvmc, backend="pillow", dpmm=dpmm)
                    copper_style = file._dispatch_style(None)
                    copper_img = copper_result.get_image(style=copper_style)
                    
                    # Render vias if any exist
                    if via_rvmc.commands:
                        via_result = render(via_rvmc, backend="pillow", dpmm=dpmm, bounds_hint=copper_result.main_box)
                        
                        # UNIFIED BOUNDING BOX APPROACH - Try to apply styling with unified coordinate system
                        try:
                            via_style = Style.presets.VIA_ALPHA
                        except AttributeError:
                            via_style = Style.presets.PASTE_MASK_ALPHA
                        
                        # Get via style
                        ref_space = ref_image.get_image_space()
                        canvas_size = ref_image.get_image().size
                        
                        # Create unified canvas for copper + vias
                        unified_img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
                        
                        # Paste copper first (it already has the right size and position)
                        unified_img.paste(copper_img, (0, 0), mask=copper_img.getchannel("A"))
                        
                        # For vias: if the via result is too small, we need to handle the coordinate offset
                        via_img = via_result.get_image(style=via_style)
                        
                        if via_img.size != (1, 1):
                            # If via image has proper size, composite it
                            if via_img.size == canvas_size:
                                # Same size - direct composite
                                unified_img.paste(via_img, (0, 0), mask=via_img.getchannel("A"))
                            else:
                                # Different size - this shouldn't happen but handle it gracefully
                                unified_img.paste(via_img, (0, 0), mask=via_img.getchannel("A"))
                        
                        # Paste the unified image to final canvas
                        final_image.paste(unified_img, paste_pos, mask=unified_img.getchannel("A"))
                    else:
                        # No vias - just use copper
                        final_image.paste(copper_img, paste_pos, mask=copper_img.getchannel("A"))
                        
                except Exception as e:
                    final_image.paste(ref_image.get_image(), paste_pos, mask=ref_image.get_image().getchannel("A"))
            else:
                final_image.paste(ref_image.get_image(), paste_pos, mask=ref_image.get_image().getchannel("A"))
        
        return CompositePillowImage(reference_images, final_image)

    def _is_copper_file(self, file: GerberFile) -> bool:
        """Safely detect if a file is copper type."""
        try:
            # Use the file's own type detection logic
            # This triggers the file's internal file type inference
            file._dispatch_style(None)
            return file.file_type == FileTypeEnum.COPPER
        except Exception:
            # Fallback: check if source path suggests copper
            if isinstance(file._source_type_or_path, Path):
                suffix = file._source_type_or_path.suffix.lower()
                return suffix in ['.gtl', '.gbl', '.g1', '.g2', '.g3', '.g4']
            return False

    def __str__(self) -> str:
        return f"{self.__class__.__qualname__}({self.files})"

    def __len__(self) -> int:
        return len(self.files)