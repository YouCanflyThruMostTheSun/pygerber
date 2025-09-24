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

        # DEBUG: Print what files we're processing
        print(f"CompositeView processing {len(self.files)} files")
        
        # STEP 1: Render all files normally first for bounding box calculation
        # This ensures alignment works exactly like before
        print(f"=== STEP 1: REFERENCE RENDERING ===")
        reference_images: list[PillowImage] = []
        for i, file in enumerate(self.files):
            print(f"  Rendering reference image {i}: {file._source_type_or_path}")
            ref_img = file.render_with_pillow(dpmm=dpmm)
            reference_images.append(ref_img)
        print(f"=== STEP 1 COMPLETE: {len(reference_images)} reference images ===")
        
        # STEP 2: Calculate bounding box using reference images with full debugging
        if not reference_images:
            return CompositePillowImage([], Image.new("RGBA", (1, 1)))

        print(f"=== STEP 2: BOUNDING BOX CALCULATION ===")
        for i, img in enumerate(reference_images):
            space = img.get_image_space()
            print(f"Image {i} ({self.files[i]._source_type_or_path}):")
            print(f"  -> units: {space.units}")
            print(f"  -> mm coords: min_x={space.min_x:.2f}, max_x={space.max_x:.2f}, min_y={space.min_y:.2f}, max_y={space.max_y:.2f}")
            print(f"  -> pixels: min_x={space.min_x_pixels}, max_x={space.max_x_pixels}, min_y={space.min_y_pixels}, max_y={space.max_y_pixels}")
            print(f"  -> image size: {img.get_image().size}")

        max_x_image = max(reference_images, key=lambda x: x.get_image_space().max_x)
        max_y_image = max(reference_images, key=lambda x: x.get_image_space().max_y)
        min_x_image = min(reference_images, key=lambda x: x.get_image_space().min_x)
        min_y_image = min(reference_images, key=lambda x: x.get_image_space().min_y)

        print(f"Selected bounds from images:")
        print(f"  -> min_x from image: {reference_images.index(min_x_image)} = {min_x_image.get_image_space().min_x:.2f}")
        print(f"  -> max_x from image: {reference_images.index(max_x_image)} = {max_x_image.get_image_space().max_x:.2f}")
        print(f"  -> min_y from image: {reference_images.index(min_y_image)} = {min_y_image.get_image_space().min_y:.2f}")
        print(f"  -> max_y from image: {reference_images.index(max_y_image)} = {max_y_image.get_image_space().max_y:.2f}")

        width_pixels = (
            max_x_image.get_image_space().max_x_pixels
            - min_x_image.get_image_space().min_x_pixels
        )
        height_pixels = (
            max_y_image.get_image_space().max_y_pixels
            - min_y_image.get_image_space().min_y_pixels
        )

        print(f"Final canvas size: {width_pixels} x {height_pixels} pixels")
        print(f"=== STEP 2 COMPLETE ===")

        final_image = Image.new("RGBA", (width_pixels, height_pixels))

        # STEP 3: Process each file for final compositing with full debugging
        print(f"=== STEP 3: FINAL COMPOSITING ===")
        for i, file in enumerate(self.files):
            print(f"Processing file {i}: {file._source_type_or_path}")
            
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
            print(f"  -> Calculated paste position: {paste_pos}")
            
            # Check if this is a copper file that needs via separation
            if self._is_copper_file(file):
                print(f"  -> Detected as COPPER file, using dual rendering")
                
                # Use the new dual-output API directly
                try:
                    # Check if the method exists
                    if hasattr(file, '_compile_and_get_rvmcs'):
                        result = file._compile_and_get_rvmcs()
                        if result is None:
                            print(f"  -> _compile_and_get_rvmcs() returned None!")
                            raise Exception("Method returned None")
                        main_rvmc, via_rvmc = result
                    else:
                        print(f"  -> _compile_and_get_rvmcs() method doesn't exist!")
                        raise Exception("Method doesn't exist")
                    
                    print(f"  -> Got main_rvmc with {len(main_rvmc.commands)} commands")
                    print(f"  -> Got via_rvmc with {len(via_rvmc.commands)} commands")
                    
                    # Render main copper using same bounding box as reference
                    copper_result = render(main_rvmc, backend="pillow", dpmm=dpmm)
                    copper_style = file._dispatch_style(None)
                    copper_img = copper_result.get_image(style=copper_style)
                    
                    print(f"  -> Copper result box: {copper_result.main_box}")
                    print(f"  -> Copper image size: {copper_img.size}")
                    
                    # Render vias if any exist - with DETAILED debugging
                    if via_rvmc.commands:
                        print(f"  -> DEBUGGING via_rvmc contents:")
                        print(f"     -> Total commands: {len(via_rvmc.commands)}")
                        
                        # Check what types of commands are in via_rvmc
                        command_types = {}
                        aperture_refs = set()
                        
                        for cmd_idx, cmd in enumerate(via_rvmc.commands):
                            cmd_type = type(cmd).__name__
                            command_types[cmd_type] = command_types.get(cmd_type, 0) + 1
                            
                            # If it's a paste command, check what aperture it references
                            if hasattr(cmd, 'source_layer_id'):
                                aperture_refs.add(cmd.source_layer_id.id)
                            
                            # Show first few commands in detail
                            if cmd_idx < 5:
                                print(f"     -> Command {cmd_idx}: {cmd_type}")
                                if hasattr(cmd, 'source_layer_id'):
                                    print(f"        -> References aperture: {cmd.source_layer_id.id}")
                                if hasattr(cmd, 'center'):
                                    print(f"        -> Center: {cmd.center}")
                        
                        print(f"     -> Command type summary: {command_types}")
                        print(f"     -> Apertures referenced by via commands: {aperture_refs}")

                        # Show coordinates of first few paste commands
                        paste_commands = [cmd for cmd in via_rvmc.commands if hasattr(cmd, 'center')]
                        print(f"     -> First 5 via coordinates:")
                        for cmd_idx, cmd in enumerate(paste_commands[:5]):
                            print(f"        -> Via {cmd_idx}: center={cmd.center}, aperture={cmd.source_layer_id.id}")

                        # Debug the Shape commands (aperture definitions)
                        shape_commands = [cmd for cmd in via_rvmc.commands if type(cmd).__name__ == 'Shape']
                        print(f"     -> Shape command details:")
                        for shape_idx, shape in enumerate(shape_commands):
                            print(f"        -> Shape {shape_idx} detailed geometry:")
                            for geom_idx, geom_cmd in enumerate(shape.commands):
                                if hasattr(geom_cmd, 'start') and hasattr(geom_cmd, 'end'):
                                    print(f"           -> Arc {geom_idx}: start={geom_cmd.start}, end={geom_cmd.end}")
                                if hasattr(geom_cmd, 'center'):
                                    print(f"           -> Arc {geom_idx}: center={geom_cmd.center}")
                                if hasattr(geom_cmd, 'get_radius'):
                                    try:
                                        radius = geom_cmd.get_radius()
                                        print(f"           -> Arc {geom_idx}: radius={radius}")
                                    except:
                                        print(f"           -> Arc {geom_idx}: radius=ERROR getting radius")
                        for shape_idx, shape in enumerate(shape_commands):
                            print(f"        -> Shape {shape_idx}: is_negative={shape.is_negative}")
                            print(f"        -> Shape {shape_idx}: commands count={len(shape.commands)}")
                            if hasattr(shape, 'metadata'):
                                print(f"        -> Shape {shape_idx}: metadata={shape.metadata}")
                            
                            # Check if shape has any actual geometry
                            if len(shape.commands) == 0:
                                print(f"        -> WARNING: Shape {shape_idx} has no geometry commands!")
                            else:
                                print(f"        -> Shape {shape_idx}: first geometry command={type(shape.commands[0]).__name__}")
                        
                        # Check if all coordinates are (0,0)
                        all_centers = [cmd.center for cmd in paste_commands]
                        unique_centers = set((c.x, c.y) for c in all_centers)
                        print(f"     -> Unique via centers: {len(unique_centers)} different positions")
                        if len(unique_centers) <= 3:  # Show them if there are only a few
                            print(f"     -> Centers: {unique_centers}")
                        
                        # NOW let's see what happens when we try to render it
                        print(f"  -> Attempting to render via_rvmc...")
                        
                        via_result = render(via_rvmc, backend="pillow", dpmm=dpmm, bounds_hint=copper_result.main_box)
                        print(f"  -> Via result box: {via_result.main_box}")
                        print(f"  -> Via result success: {via_result.is_success()}")
                        
                        # Try to get the raw image before styling
                        if via_result.image:
                            raw_via_img = via_result.get_image_no_style()
                            print(f"  -> Raw via image size: {raw_via_img.size}")
                            print(f"  -> Raw via image mode: {raw_via_img.mode}")
                            raw_via_img.save(f"debug_raw_via_{i}.png")
                            print(f"  -> Saved debug_raw_via_{i}.png")
                        else:
                            print(f"  -> WARNING: via_result.image is None!")
                        
                        # UNIFIED BOUNDING BOX APPROACH - Try to apply styling with unified coordinate system
                        print(f"  -> Using unified coordinate system approach...")
                        
                        try:
                            via_style = Style.presets.VIA_ALPHA
                            print(f"  -> Using VIA_ALPHA preset")
                        except AttributeError:
                            via_style = Style.presets.PASTE_MASK_ALPHA
                            print(f"  -> VIA_ALPHA not found, using PASTE_MASK_ALPHA as fallback")
                        
                        # Get via style
                        ref_space = ref_image.get_image_space()
                        canvas_size = ref_image.get_image().size
                        
                        print(f"  -> Creating unified canvas size: {canvas_size}")
                        
                        # Create unified canvas for copper + vias
                        unified_img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
                        
                        # Paste copper first (it already has the right size and position)
                        unified_img.paste(copper_img, (0, 0), mask=copper_img.getchannel("A"))
                        print(f"  -> Added copper to unified canvas")
                        
                        # For vias: if the via result is too small, we need to handle the coordinate offset
                        via_img = via_result.get_image(style=via_style)
                        print(f"  -> Via image size: {via_img.size}, Copper image size: {copper_img.size}")
                        
                        if via_img.size == (1, 1):
                            print(f"  -> WARNING: Via image is 1x1, likely due to empty bounding box")
                            print(f"  -> This means vias exist but aren't being rendered properly")
                        else:
                            # If via image has proper size, composite it
                            if via_img.size == canvas_size:
                                # Same size - direct composite
                                unified_img.paste(via_img, (0, 0), mask=via_img.getchannel("A"))
                                print(f"  -> Composited via image directly")
                            else:
                                # Different size - this shouldn't happen but handle it gracefully
                                print(f"  -> Size mismatch: via {via_img.size} vs canvas {canvas_size}")
                                unified_img.paste(via_img, (0, 0), mask=via_img.getchannel("A"))
                        
                        # Save debug images
                        copper_img.save(f"debug_copper_{i}.png")
                        via_img.save(f"debug_via_{i}.png")
                        unified_img.save(f"debug_unified_{i}.png")
                        print(f"  -> Saved debug images")
                        
                        # Paste the unified image to final canvas
                        final_image.paste(unified_img, paste_pos, mask=unified_img.getchannel("A"))
                        print(f"  -> Pasted unified copper+via image at {paste_pos}")
                    else:
                        # No vias - just use copper
                        final_image.paste(copper_img, paste_pos, mask=copper_img.getchannel("A"))
                        print(f"  -> No via commands found, pasted copper only")
                        
                except Exception as e:
                    print(f"  -> Dual rendering failed: {e}, using reference image")
                    final_image.paste(ref_image.get_image(), paste_pos, mask=ref_image.get_image().getchannel("A"))
            else:
                print(f"  -> Not copper, using reference image")
                final_image.paste(ref_image.get_image(), paste_pos, mask=ref_image.get_image().getchannel("A"))

        print(f"=== STEP 3 COMPLETE: Final compositing done ===")
        
        # Save final debug image
        final_image.save("debug_final_composite.png")
        print(f"Saved debug_final_composite.png")
        
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