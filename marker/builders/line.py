from copy import deepcopy
from typing import Annotated, List, Optional, Tuple

import numpy as np
from ftfy import fix_text
from PIL import Image

from surya.detection import DetectionPredictor, InlineDetectionPredictor, TextDetectionResult
from surya.ocr_error import OCRErrorPredictor

from marker.builders import BaseBuilder
from marker.providers import ProviderOutput, ProviderPageLines
from marker.providers.pdf import PdfProvider
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.groups.page import PageGroup
from marker.schema.polygon import PolygonBox
from marker.schema.registry import get_block_class
from marker.schema.text.line import Line
from marker.settings import settings
from marker.util import matrix_intersection_area

class TextBox(PolygonBox):
    math: bool = False

    def __hash__(self):
        return hash(tuple(self.bbox))

class LineBuilder(BaseBuilder):
    """
    A builder for detecting text lines, and inline math. Merges the detected lines with the lines from the provider
    """
    detection_batch_size: Annotated[
        Optional[int],
        "The batch size to use for the detection model.",
        "Default is None, which will use the default batch size for the model."
    ] = None
    ocr_error_batch_size: Annotated[
        Optional[int],
        "The batch size to use for the ocr error detection model.",
        "Default is None, which will use the default batch size for the model."
    ] = None
    enable_table_ocr: Annotated[
        bool,
        "Whether to skip OCR on tables.  The TableProcessor will re-OCR them.  Only enable if the TableProcessor is not running.",
    ] = False
    layout_coverage_min_lines: Annotated[
        int,
        "The minimum number of PdfProvider lines that must be covered by the layout model",
        "to consider the lines from the PdfProvider valid.",
    ] = 1
    layout_coverage_threshold: Annotated[
        float,
        "The minimum coverage ratio required for the layout model to consider",
        "the lines from the PdfProvider valid.",
    ] = .25
    detected_provider_line_overlap: Annotated[
        float,
        "The maximum overlap between a detected text line and a provider line to consider as a new line"
    ] = .3
    span_inline_math_overlap_threshold: Annotated[
        float,
        "The minimum overlap of a span with an inline math box to consider for removal"
    ] = .5
    char_inline_math_overlap_threshold: Annotated[
        float,
        "The minimum overlap of a character with an inline math box to consider for removal"
    ] = .5
    line_inline_math_overlap_threshold: Annotated[
        float,
        "The minimum overlap of a line with an inline math box to consider as a match"
    ] = 0.
    line_text_overlap_threshold: Annotated[
        float,
        "The minimum overlap of an equation with a text line to consider as a match"
    ] = .5
    inline_math_minimum_area: Annotated[
        float,
        "The minimum area for an inline math block, in pixels."
    ] = 20
    inline_math_line_vertical_merge_threshold: Annotated[
        int,
        "The maximum pixel distance between y1s for two lines to be merged"
    ] = 5
    excluded_for_coverage: Annotated[
        Tuple[BlockTypes],
        "A list of block types to exclude from the layout coverage check.",
    ] = (BlockTypes.Figure, BlockTypes.Picture, BlockTypes.Table, BlockTypes.FigureGroup, BlockTypes.TableGroup, BlockTypes.PictureGroup)
    use_llm: Annotated[
        bool,
        "Whether to use the LLM model for advanced processing."
    ] = False
    texify_inline_spans: Annotated[
        bool,
        "Whether to run texify on inline math spans."
    ] = False

    def __init__(self, detection_model: DetectionPredictor, inline_detection_model: InlineDetectionPredictor, ocr_error_model: OCRErrorPredictor, config=None):
        super().__init__(config)

        self.detection_model = detection_model
        self.inline_detection_model = inline_detection_model
        self.ocr_error_model = ocr_error_model

    def __call__(self, document: Document, provider: PdfProvider):
        # Disable Inline Detection for documents where layout model doesn't detect any equations
        # Also disable if we won't use the inline detections (if we aren't using the LLM or texify)
        do_inline_math_detection = document.contained_blocks([BlockTypes.Equation]) and (self.texify_inline_spans or self.use_llm)
        provider_lines, ocr_lines = self.get_all_lines(document, provider, do_inline_math_detection)
        self.merge_blocks(document, provider_lines, ocr_lines)

    def get_detection_batch_size(self):
        if self.detection_batch_size is not None:
            return self.detection_batch_size
        elif settings.TORCH_DEVICE_MODEL == "cuda":
            return 4
        return 4

    def get_ocr_error_batch_size(self):
        if self.ocr_error_batch_size is not None:
            return self.ocr_error_batch_size
        elif settings.TORCH_DEVICE_MODEL == "cuda":
            return 4
        return 4

    def get_detection_results(self, page_images: List[Image.Image], run_detection: List[bool], do_inline_math_detection: bool):
        page_detection_results = self.detection_model(
            images=[p for p, good in zip(page_images, run_detection) if good],
            batch_size=self.get_detection_batch_size()
        )
        detection_results = []
        idx = 0
        for good in run_detection:
            if good:
                detection_results.append(page_detection_results[idx])
                idx += 1
            else:
                detection_results.append(None)
        assert idx == len(page_detection_results)

        inline_detection_results = [None] * len(page_images)
        if do_inline_math_detection:
            inline_detection_results = self.inline_detection_model(
                images=page_images,
                text_boxes=[[b.bbox for b in det_result.bboxes] for det_result in detection_results],
                batch_size=self.get_detection_batch_size()
            )

        return detection_results, inline_detection_results


    def get_all_lines(self, document: Document, provider: PdfProvider, do_inline_math_detection: bool):
        ocr_error_detection_results = self.ocr_error_detection(document.pages, provider.page_lines)

        boxes_to_ocr = {page.page_id: [] for page in document.pages}
        page_lines = {page.page_id: [] for page in document.pages}

        LineClass: Line = get_block_class(BlockTypes.Line)

        layout_good = []
        for document_page, ocr_error_detection_label in zip(document.pages, ocr_error_detection_results.labels):
            provider_lines: List[ProviderOutput] = provider.page_lines.get(document_page.page_id, [])
            provider_lines_good = all([
                bool(provider),
                ocr_error_detection_label != 'bad',
                self.check_layout_coverage(document_page, provider_lines)
            ])
            layout_good.append(provider_lines_good)

        run_detection = [not good or do_inline_math_detection for good in layout_good]
        page_images = [page.get_image(highres=False, remove_tables=not self.enable_table_ocr) for page, good in zip(document.pages, run_detection) if good]
        detection_results, inline_detection_results = self.get_detection_results(page_images, run_detection, do_inline_math_detection)

        for document_page, detection_result, inline_detection_result, provider_lines_good in zip(
                document.pages,
                detection_results,
                inline_detection_results,
                layout_good
        ):
            provider_lines: List[ProviderOutput] = provider.page_lines.get(document_page.page_id, [])
            page_size = provider.get_page_bbox(document_page.page_id).size
            image_size = PolygonBox.from_bbox(detection_result.image_bbox).size if detection_result else page_size

            # Filter out detected equation blocks
            inline_detection_result = self.filter_equation_overlaps(
                document,
                document_page,
                inline_detection_result,
                image_size,
                page_size,
                self.line_inline_math_overlap_threshold
            )
            detection_result = self.filter_equation_overlaps(
                document,
                document_page,
                detection_result,
                image_size,
                page_size,
                self.line_text_overlap_threshold
            )

            # Merge text and inline math detection results
            merged_detection_boxes = self.determine_math_lines(text_result=detection_result, inline_result=inline_detection_result)
            math_detection_boxes = [(i, box) for i, box in enumerate(merged_detection_boxes) if box.math]
            nonmath_detection_boxes = [(i, box) for i, box in enumerate(merged_detection_boxes) if not box.math]

            if provider_lines_good:
                # Merge inline math blocks into the provider lines, only persist new detected text lines which do not overlap with existing provider lines
                # The missing lines are not from a table, so we can safely set this - The attribute for individual blocks is overridden by OCRBuilder
                document_page.text_extraction_method = 'pdftext'

                # Add in the provider lines - merge ones that get broken by inline math
                page_lines[document_page.page_id].extend(
                    self.merge_provider_lines_inline_math(
                        provider_lines,
                        [b for _,b in math_detection_boxes],
                        image_size,
                        page_size
                    )
                )
            else:
                document_page.text_extraction_method = 'surya'

                # Sort lines properly
                full_lines = nonmath_detection_boxes + math_detection_boxes
                full_lines = sorted(full_lines, key=lambda x: x[0])
                full_lines = [b for _, b in full_lines]

                # Skip inline math merging if no provider lines are good; OCR all text lines and all inline math lines
                boxes_to_ocr[document_page.page_id].extend(full_lines)

        # Dummy lines to merge into the document - Contains no spans, will be filled in later by OCRBuilder
        ocr_lines = {document_page.page_id: [] for document_page in document.pages}
        for page_id, page_ocr_boxes in boxes_to_ocr.items():
            page_size = provider.get_page_bbox(page_id).size
            image_size = document.get_page(page_id).get_image(highres=False).size
            for box_to_ocr in page_ocr_boxes:
                line_polygon = PolygonBox(polygon=box_to_ocr.polygon).rescale(image_size, page_size)
                format = ["math"] if box_to_ocr.math else None
                ocr_lines[page_id].append(
                    ProviderOutput(
                        line=LineClass(
                            polygon=line_polygon,
                            page_id=page_id,
                            text_extraction_method='surya',
                            formats=format
                        ),
                        spans=[]
                    )
                )

        return page_lines, ocr_lines

    def ocr_error_detection(self, pages:List[PageGroup], provider_page_lines: ProviderPageLines):
        page_texts = []
        for document_page in pages:
            provider_lines = provider_page_lines.get(document_page.page_id, [])
            page_text = '\n'.join(' '.join(s.text for s in line.spans) for line in provider_lines)
            page_texts.append(page_text)

        ocr_error_detection_results = self.ocr_error_model(
            page_texts,
            batch_size=int(self.get_ocr_error_batch_size())
        )
        return ocr_error_detection_results

    def check_layout_coverage(
        self,
        document_page: PageGroup,
        provider_lines: List[ProviderOutput],
    ):
        covered_blocks = 0
        total_blocks = 0
        large_text_blocks = 0

        layout_blocks = [document_page.get_block(block) for block in document_page.structure]
        layout_blocks = [b for b in layout_blocks if b.block_type not in self.excluded_for_coverage]

        layout_bboxes = [block.polygon.bbox for block in layout_blocks]
        provider_bboxes = [line.line.polygon.bbox for line in provider_lines]

        if len(layout_bboxes) == 0:
            return True

        if len(provider_bboxes) == 0:
            return False

        intersection_matrix = matrix_intersection_area(layout_bboxes, provider_bboxes)

        for idx, layout_block in enumerate(layout_blocks):
            total_blocks += 1
            intersecting_lines = np.count_nonzero(intersection_matrix[idx] > 0)

            if intersecting_lines >= self.layout_coverage_min_lines:
                covered_blocks += 1

            if layout_block.polygon.intersection_pct(document_page.polygon) > 0.8 and layout_block.block_type == BlockTypes.Text:
                large_text_blocks += 1

        coverage_ratio = covered_blocks / total_blocks if total_blocks > 0 else 1
        text_okay = coverage_ratio >= self.layout_coverage_threshold

        # Model will sometimes say there is a single block of text on the page when it is blank
        if not text_okay and (total_blocks == 1 and large_text_blocks == 1):
            text_okay = True
        return text_okay

    def merge_blocks(self, document: Document, page_provider_lines: ProviderPageLines, page_ocr_lines: ProviderPageLines):
        for document_page in document.pages:
            provider_lines = page_provider_lines[document_page.page_id]
            ocr_lines = page_ocr_lines[document_page.page_id]

            # Only one or the other will have lines
            merged_lines = provider_lines + ocr_lines

            # Text extraction method is overridden later for OCRed documents
            document_page.merge_blocks(merged_lines, text_extraction_method='pdftext')

    def filter_equation_overlaps(
            self,
            document,
            page: PageGroup,
            inline_boxes: TextDetectionResult,
            image_size,
            page_size,
            threshold: float
    ):
        if inline_boxes is None:
            return inline_boxes

        equations = page.contained_blocks(document, (BlockTypes.Equation,))
        equation_boxes = [eq.polygon.bbox for eq in equations]
        inline_polys = [PolygonBox(polygon=box.polygon).rescale(image_size, page_size) for box in inline_boxes.bboxes]
        inline_bboxes = [poly.bbox for poly in inline_polys]
        inline_areas = [poly.area for poly in inline_polys]

        if len(equation_boxes) == 0 or len(inline_bboxes) == 0:
            return inline_boxes

        overlaps = matrix_intersection_area(inline_bboxes, equation_boxes)
        overlap_idxs = (np.max(overlaps, axis=-1) / np.array(inline_areas)) > threshold
        inline_boxes.bboxes = [ib for i, ib in enumerate(inline_boxes.bboxes) if not overlap_idxs[i]]
        return inline_boxes


    def determine_math_lines(
        self,
        text_result: TextDetectionResult,
        inline_result: TextDetectionResult,
        math_box_padding: float = .05
    ) -> List[TextBox]:
        """
        Marks lines as math if they contain inline math boxes.
        """

        if not text_result:
            return []

        text_boxes = [
            TextBox(
                polygon=box.polygon
            ) for box in text_result.bboxes
        ]
        
        # Skip if no inline math was detected
        if not inline_result:
            return text_boxes

        inline_bboxes = [m.bbox for m in inline_result.bboxes]
        text_bboxes = [t.bbox for t in text_boxes]

        if len(inline_bboxes) == 0:
            return text_boxes

        if len(text_boxes) == 0:
            return []

        overlaps = matrix_intersection_area(inline_bboxes, text_bboxes)

        # Mark text boxes as math if they overlap with an inline math box
        for i, inline_box in enumerate(inline_result.bboxes):
            overlap_row = overlaps[i]
            max_overlap_idx = np.argmax(overlap_row)
            max_overlap_box = text_boxes[max_overlap_idx]

            max_overlap = np.max(overlap_row) / inline_box.area

            # Avoid small or nonoverlapping inline math regions
            if max_overlap <= self.line_inline_math_overlap_threshold or inline_box.area < self.inline_math_minimum_area:
                continue

            # Ignore vertical lines
            if max_overlap_box.height > max_overlap_box.width:
                continue

            max_overlap_box.math = True

        return text_boxes

    # Add appropriate formats to math spans added by inline math detection
    def add_math_span_format(self, provider_line):
        if not provider_line.line.formats:
            provider_line.line.formats = ["math"]
        elif "math" not in provider_line.line.formats:
            provider_line.line.formats.append("math")

    def merge_provider_lines_inline_math(
            self,
            provider_lines: List[ProviderOutput],
            inline_math_lines: List[TextBox],
            image_size,
            page_size
    ):
        # When provider lines is empty or no inline math detected, return provider lines
        if not provider_lines or not inline_math_lines:
            return provider_lines

        horizontal_provider_lines = [
            (j, provider_line) for j, provider_line in enumerate(provider_lines)
            if provider_line.line.polygon.height < provider_line.line.polygon.width * 3 # Multiply to account for small blocks inside equations, but filter out big vertical lines
        ]
        provider_line_boxes = [p.line.polygon.bbox for _, p in horizontal_provider_lines]
        math_line_boxes = [PolygonBox(polygon=m.polygon).rescale(image_size, page_size).bbox for m in inline_math_lines]

        overlaps = matrix_intersection_area(math_line_boxes, provider_line_boxes)

        # Find potential merges
        merge_lines = []
        for i in range(len(math_line_boxes)):
            merge_line = []
            math_line_polygon = PolygonBox(polygon=inline_math_lines[i].polygon).rescale(image_size, page_size)
            max_overlap = np.max(overlaps[i])
            if max_overlap <= self.line_inline_math_overlap_threshold:
                continue

            best_overlap = np.argmax(overlaps[i])
            best_overlap_line = horizontal_provider_lines[best_overlap]
            best_overlap_y1 = best_overlap_line[1].line.polygon.y_start

            nonzero_idxs = np.nonzero(overlaps[i] > self.line_inline_math_overlap_threshold)[0]
            for idx in nonzero_idxs:
                provider_idx, provider_line = horizontal_provider_lines[idx]
                provider_line_y1 = provider_line.line.polygon.y_start

                should_merge_line = False
                if abs(provider_line_y1 - best_overlap_y1) <= self.inline_math_line_vertical_merge_threshold:
                    should_merge_line = True

                line_overlaps = self.find_overlapping_math_chars(provider_line, math_line_polygon, remove_chars=not should_merge_line)

                # Do not merge if too far above/below (but remove characters)
                if line_overlaps and should_merge_line:
                    # Add the index of the provider line to the merge line
                    merge_line.append(provider_idx)

            if len(merge_line) > 0:
                merge_lines.append(merge_line)

        # Handle the merging
        already_merged = set()
        potential_merges = set([m for merge_line in merge_lines for m in merge_line])
        out_provider_lines = [(i, p) for i, p in enumerate(provider_lines) if i not in potential_merges]
        for merge_section in merge_lines:
            merge_section = [m for m in merge_section if m not in already_merged]
            if len(merge_section) == 0:
                continue
            elif len(merge_section) == 1:
                line_idx = merge_section[0]
                merged_line = provider_lines[line_idx]
                self.add_math_span_format(merged_line)
                out_provider_lines.append((line_idx, merged_line))
                already_merged.add(merge_section[0])
                continue

            merge_section = sorted(merge_section)
            merged_line = None
            min_idx = min(merge_section)
            for idx in merge_section:
                provider_line = deepcopy(provider_lines[idx])
                if merged_line is None:
                    merged_line = provider_line
                else:
                    # Combine the spans of the provider line with the merged line
                    merged_line = merged_line.merge(provider_line)
                    self.add_math_span_format(merged_line)
                already_merged.add(idx) # Prevent double merging
            out_provider_lines.append((min_idx, merged_line))

        # Sort to preserve original order
        out_provider_lines = sorted(out_provider_lines, key=lambda x: x[0])
        out_provider_lines = [p for _, p in out_provider_lines]
        return out_provider_lines

    def clear_line_text(self, provider_line):
        for span in provider_line.spans:
            span.text = ""

    def find_overlapping_math_chars(self, provider_line, math_line_polygon, remove_chars=False):
        # Identify if a character in the provider line overlaps with the inline math line - meaning that the line can be treated as math
        spans = provider_line.spans
        math_overlaps = False

        # For providers which do not surface characters
        if provider_line.chars is None:
            for span in spans:
                if span.polygon.intersection_pct(math_line_polygon) > self.span_inline_math_overlap_threshold:
                    math_overlaps = True
            return math_overlaps

        # For providers which surface characters - find line overlap based on characters
        assert len(spans) == len(provider_line.chars), "Number of spans and characters in provider line do not match"
        for span, span_chars in zip(spans, provider_line.chars):
            if len(span_chars) == 0:
                continue

            char_intersections_areas = matrix_intersection_area([char.polygon.bbox for char in span_chars], [math_line_polygon.bbox]).max(axis=-1)
            char_intersections = char_intersections_areas / np.array([char.polygon.area for char in span_chars])

            new_span_chars = []
            span_overlaps = False
            for char, intersection_pct in zip(span_chars, char_intersections):
                if intersection_pct >= self.char_inline_math_overlap_threshold:
                    span_overlaps = True
                else:
                    new_span_chars.append(char)

            # Remove stray characters that overlap with math lines
            if span_overlaps and remove_chars:
                span.text = fix_text(''.join(c.char for c in new_span_chars))

            math_overlaps = math_overlaps or span_overlaps

        return math_overlaps