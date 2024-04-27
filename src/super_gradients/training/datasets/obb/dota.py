import dataclasses
import multiprocessing
from functools import partial
from pathlib import Path
from typing import Tuple, Union, Optional, List, Iterable

import cv2
import numpy as np
import torch
from super_gradients.common.registry import register_dataset, register_collate_function
from super_gradients.dataset_interfaces import HasClassesInformation
from torch.utils.data import Dataset
from tqdm import tqdm

__all__ = ["OBBSample", "OrientedBoxesCollate", "DOTAOBBDataset"]


@dataclasses.dataclass
class OBBSample:
    """
    A data class describing a single object detection sample that comes from a dataset.
    It contains both input image and target information to train an object detection model.

    :param image:              Associated image with a sample. Can be in [H,W,C] or [C,H,W] format
    :param boxes_cxcywhr:      Numpy array of [N,5] shape with oriented bounding box of each instance (CX,CY,W,H,R)
    :param labels:             Numpy array of [N] shape with class label for each instance
    :param is_crowd:           (Optional) Numpy array of [N] shape with is_crowd flag for each instance
    :param additional_samples: (Optional) List of additional samples for the same image.
    """

    __slots__ = ["image", "rboxes_cxcywhr", "labels", "is_crowd", "additional_samples"]

    image: Union[np.ndarray, torch.Tensor]
    rboxes_cxcywhr: np.ndarray
    labels: np.ndarray
    is_crowd: np.ndarray
    additional_samples: Optional[List["OBBSample"]]

    def __init__(
        self,
        image: Union[np.ndarray, torch.Tensor],
        boxes_cxcywhr: np.ndarray,
        labels: np.ndarray,
        is_crowd: Optional[np.ndarray] = None,
        additional_samples: Optional[List["OBBSample"]] = None,
    ):
        if is_crowd is None:
            is_crowd = np.zeros(len(labels), dtype=bool)

        if len(boxes_cxcywhr) != len(labels):
            raise ValueError("Number of bounding boxes and labels must be equal. Got {len(bboxes_xyxy)} and {len(labels)} respectively")

        if len(boxes_cxcywhr) != len(is_crowd):
            raise ValueError("Number of bounding boxes and is_crowd flags must be equal. Got {len(bboxes_xyxy)} and {len(is_crowd)} respectively")

        if len(boxes_cxcywhr.shape) != 2 or boxes_cxcywhr.shape[1] != 5:
            raise ValueError(f"Oriented boxes must be in [N,5] format. Shape of input bboxes is {boxes_cxcywhr.shape}")

        if len(is_crowd.shape) != 1:
            raise ValueError(f"Number of is_crowd flags must be in [N] format. Shape of input is_crowd is {is_crowd.shape}")

        if len(labels.shape) != 1:
            raise ValueError("Labels must be in [N] format. Shape of input labels is {labels.shape}")

        self.image = image
        self.rboxes_cxcywhr = boxes_cxcywhr
        self.labels = labels
        self.is_crowd = is_crowd
        self.additional_samples = additional_samples
        self.sanitize_sample()

    def sanitize_sample(self) -> "OBBSample":
        """
        Apply sanity checks on the detection sample, which includes clamping of bounding boxes to image boundaries.
        This function does not remove instances, but may make them subject for removal later on.
        This method operates in-place and modifies the caller.
        :return: A DetectionSample after filtering (caller instance).
        """
        # image_height, image_width = self.image.shape[:2]
        # self.bboxes_xyxy = change_bbox_bounds_for_image_size_inplace(self.bboxes_xyxy, img_shape=(image_height, image_width))
        self.filter_by_bbox_area(0)
        return self

    def filter_by_mask(self, mask: np.ndarray) -> "OBBSample":
        """
        Remove boxes & labels with respect to a given mask.
        This method operates in-place and modifies the caller.
        If you are implementing a subclass of DetectionSample and adding extra field associated with each bbox
        instance (Let's say you add a distance property for each bbox from the camera), then you should override
        this method to do filtering on extra attribute as well.

        :param mask:   A boolean or integer mask of samples to keep for given sample.
        :return:       A DetectionSample after filtering (caller instance).
        """
        self.rboxes_cxcywhr = self.rboxes_cxcywhr[mask]
        self.labels = self.labels[mask]
        if self.is_crowd is not None:
            self.is_crowd = self.is_crowd[mask]
        return self

    def filter_by_bbox_area(self, min_rbox_area: Union[int, float]) -> "OBBSample":
        """
        Remove pose instances that has area of the corresponding bounding box less than a certain threshold.
        This method operates in-place and modifies the caller.

        :param min_rbox_area: Minimal rotated box area of the box to keep.
        :return:              A OBBSample after filtering (caller instance).
        """
        area = self.rboxes_cxcywhr[..., 2:4].prod(axis=-1)
        keep_mask = area > min_rbox_area
        return self.filter_by_mask(keep_mask)


@register_collate_function()
class OrientedBoxesCollate:
    def __call__(self, batch: List[OBBSample]):
        from super_gradients.training.datasets.pose_estimation_datasets.yolo_nas_pose_collate_fn import flat_collate_tensors_with_batch_index

        images = []
        all_boxes = []
        all_labels = []
        all_crowd_masks = []

        for sample in batch:
            images.append(torch.from_numpy(np.transpose(sample.image, [2, 0, 1])))
            all_boxes.append(torch.from_numpy(sample.rboxes_cxcywhr))
            all_labels.append(torch.from_numpy(sample.labels.reshape((-1, 1))))
            all_crowd_masks.append(torch.from_numpy(sample.is_crowd.reshape((-1, 1))))
            sample.image = None

        images = torch.stack(images)

        boxes = flat_collate_tensors_with_batch_index(all_boxes).float()
        labels = flat_collate_tensors_with_batch_index(all_labels).long()
        is_crowd = flat_collate_tensors_with_batch_index(all_crowd_masks)

        extras = {"gt_samples": batch}
        return images, (boxes, labels, is_crowd), extras


@register_dataset()
class DOTAOBBDataset(Dataset, HasClassesInformation):
    def __init__(
        self,
        data_dir,
        transforms,
        class_names: Iterable[str],
        ignore_empty_annotations: bool = False,
        difficult_labels_are_crowd: bool = False,
        images_subdir="images",
        ann_subdir="ann-obb",
    ):
        super().__init__()

        images_dir = Path(data_dir) / images_subdir
        ann_dir = Path(data_dir) / ann_subdir
        images, labels = self.find_images_and_labels(images_dir, ann_dir)
        self.images = []
        self.coords = []
        self.classes = []
        self.difficult = []
        self.transforms = transforms
        self.class_names = list(class_names)
        self.difficult_labels_are_crowd = difficult_labels_are_crowd

        class_names_to_index = {name: i for i, name in enumerate(self.class_names)}
        for image_path, label_path in tqdm(zip(images, labels), desc=f"Parsing annotations in {ann_dir}", total=len(images)):
            coords, classes, difficult = self.parse_annotation_file(label_path)
            if ignore_empty_annotations and len(coords) == 0:
                continue
            self.images.append(image_path)
            self.coords.append(coords)
            self.classes.append(np.array([class_names_to_index[c] for c in classes], dtype=int))
            self.difficult.append(difficult)

    def __len__(self):
        return len(self.images)

    def __getitem__(self, index) -> OBBSample:
        image = cv2.imread(str(self.images[index]))
        coords = self.coords[index]
        classes = self.classes[index]
        difficult = self.difficult[index]

        # TODO: Change this
        # Hard-coded image normalization
        # No data augmentation
        image = (image / 255).astype(np.float32)

        cxcywhr = np.array([self.poly_to_rbox(poly) for poly in coords], dtype=np.float32)

        is_crowd = difficult.reshape(-1) if self.difficult_labels_are_crowd else np.zeros_like(difficult, dtype=bool)
        sample = OBBSample(
            image=image,
            boxes_cxcywhr=cxcywhr.reshape(-1, 5),
            labels=classes.reshape(-1),
            is_crowd=is_crowd,
        )
        return sample

    def get_sample_classes_information(self, index) -> np.ndarray:
        """
        Returns a histogram of length `num_classes` with class occurrences at that index.
        """
        return np.bincount(self.classes[index], minlength=len(self.class_names))

    def get_dataset_classes_information(self) -> np.ndarray:
        """
        Returns a matrix of shape (dataset_length, num_classes). Each row `i` is histogram of length `num_classes` with class occurrences for sample `i`.
        Example implementation, assuming __len__: `np.vstack([self.get_sample_classes_information(i) for i in range(len(self))])`
        """
        m = np.zeros((len(self), len(self.class_names)), dtype=int)
        for i in range(len(self)):
            m[i] = self.get_sample_classes_information(i)
        return m

    @classmethod
    def poly_to_rbox(cls, poly):
        """
        Convert polygon to rotated bounding box
        :param poly: Input polygon in [N,2] format
        :return: Rotated box in CXCYWHR format
        """
        rect = cv2.minAreaRect(poly)
        cx, cy = rect[0]
        w, h = rect[1]
        angle = rect[2]
        return cx, cy, w, h, np.deg2rad(angle)

    @classmethod
    def find_images_and_labels(cls, images_dir, ann_dir):
        images_dir = Path(images_dir)
        ann_dir = Path(ann_dir)

        images = list(images_dir.glob("*.png"))
        labels = list(sorted(ann_dir.glob("*.txt")))

        if len(images) != len(labels):
            raise ValueError(f"Number of images and labels do not match. There are {len(images)} images and {len(labels)} labels.")

        images = []
        for label_path in labels:
            image_path = images_dir / (label_path.stem + ".png")
            if not image_path.exists():
                raise ValueError(f"Image {image_path} does not exist")
            images.append(image_path)
        return images, labels

    @classmethod
    def parse_annotation_file(cls, annotation_file: Path):
        with open(annotation_file, "r") as f:
            lines = f.readlines()

        coords = []
        classes = []
        difficult = []

        for line in lines:
            parts = line.strip().split(" ")
            if len(parts) != 10:
                raise ValueError(f"Invalid number of parts in line: {line}")

            x1, y1, x2, y2, x3, y3, x4, y4 = map(float, parts[:8])
            coords.append([[x1, y1], [x2, y2], [x3, y3], [x4, y4]])
            classes.append(parts[8])
            difficult.append(int(parts[9]))

        return np.array(coords, dtype=np.float32).reshape(-1, 4, 2), np.array(classes, dtype=np.object_), np.array(difficult, dtype=int)

    @classmethod
    def chip_image(cls, img, coords, classes, difficult, tile_size, tile_step, min_visibility=0.4, min_area=4):
        """
        Chip an image and get relative coordinates and classes.  Bounding boxes that pass into
        multiple chips are clipped: each portion that is in a chip is labeled. For example,
        half a building will be labeled if it is cut off in a chip.

        :param img: the image to be chipped in array format
        :param coords: an (N,4,2) array of oriented box coordinates for that image
        :param classes: an (N,1) array of classes for each bounding box
        :param tile_size: an (W,H) tuple indicating width and height of chips

        Output:
            An image array of shape (M,W,H,C), where M is the number of chips,
            W and H are the dimensions of the image, and C is the number of color
            channels.  Also returns boxes and classes dictionaries for each corresponding chip.
        """
        height, width, _ = img.shape

        tile_size_width, tile_size_height = tile_size
        tile_step_width, tile_step_height = tile_step

        images = []
        total_boxes = []
        total_classes = []
        total_difficult = []
        k = 0

        start_x = 0
        end_x = start_x + tile_size_width

        all_areas = np.array(list(cv2.contourArea(cv2.convexHull(poly)) for poly in coords), dtype=np.float32)

        bboxes_min_point = np.min(coords, axis=1)
        bboxes_max_point = np.max(coords, axis=1)

        while start_x < width:
            start_y = 0
            end_y = start_y + tile_size_height
            while start_y < height:
                chip = img[start_y:end_y, start_x:end_x, :3]

                # Filter out boxes that whose bounding box is definitely not in the chip
                outside_mask = np.logical_or(
                    np.any(bboxes_max_point < [start_x, start_y], axis=1),
                    np.any(bboxes_min_point > [end_x, end_y], axis=1),
                )

                visibility_mask = ~outside_mask

                visible_coords = coords[visibility_mask]
                visible_classes = classes[visibility_mask]
                visible_difficult = difficult[visibility_mask]
                visible_areas = all_areas[visibility_mask]

                out = np.stack(
                    (
                        visible_coords[:, :, 0] - start_x,
                        visible_coords[:, :, 1] - start_y,
                    ),
                    axis=2,
                )

                out_clipped = np.stack(
                    (
                        np.clip(visible_coords[:, :, 0] - start_x, 0, chip.shape[1]),
                        np.clip(visible_coords[:, :, 1] - start_y, 0, chip.shape[0]),
                    ),
                    axis=2,
                )
                areas_clipped = np.array(list(cv2.contourArea(cv2.convexHull(c)) for c in out_clipped), dtype=np.float32)

                visibility_fraction = areas_clipped / (visible_areas + 1e-6)
                visibility_mask = visibility_fraction >= min_visibility
                min_area_mask = areas_clipped >= min_area

                out = out[visibility_mask & min_area_mask]
                visible_classes = visible_classes[visibility_mask & min_area_mask]
                visible_difficult = visible_difficult[visibility_mask & min_area_mask]

                total_boxes.append(out)
                total_classes.append(visible_classes)
                total_difficult.append(visible_difficult)

                if chip.shape[0] < tile_size_height or chip.shape[1] < tile_size_width:
                    chip = cv2.copyMakeBorder(
                        chip,
                        top=0,
                        left=0,
                        bottom=tile_size_height - chip.shape[0],
                        right=tile_size_width - chip.shape[1],
                        value=0,
                        borderType=cv2.BORDER_CONSTANT,
                    )
                images.append(chip)
                k = k + 1

                start_y += tile_step_height
                end_y += tile_step_height

            start_x += tile_step_width
            end_x += tile_step_width

        return images, total_boxes, total_classes, total_difficult

    @classmethod
    def slice_dataset_into_tiles(
        cls, data_dir, output_dir, ann_subdir_name, tile_size: int, tile_step: int, scale_factors: Tuple, min_visibility, min_area, num_workers: int
    ):
        data_dir = Path(data_dir)
        input_images_dir = data_dir / "images"
        input_ann_dir = data_dir / ann_subdir_name
        images, labels = cls.find_images_and_labels(input_images_dir, input_ann_dir)

        output_dir = Path(output_dir)
        output_images_dir = output_dir / "images"
        output_ann_dir = output_dir / ann_subdir_name

        output_images_dir.mkdir(parents=True, exist_ok=True)
        output_ann_dir.mkdir(parents=True, exist_ok=True)

        with multiprocessing.Pool(num_workers) as wp:
            payload = [(image_path, ann_path, scale) for image_path, ann_path in zip(images, labels) for scale in scale_factors]

            worker_fn = partial(
                cls._worker_fn,
                tile_size=tile_size,
                tile_step=tile_step,
                min_visibility=min_visibility,
                min_area=min_area,
                output_images_dir=output_images_dir,
                output_ann_dir=output_ann_dir,
            )
            for _ in tqdm(wp.imap_unordered(worker_fn, payload), total=len(payload)):
                pass

    @classmethod
    def _worker_fn(cls, args, tile_size, tile_step, min_visibility, min_area, output_images_dir, output_ann_dir):
        image_path, ann_path, scale = args
        image = cv2.imread(str(image_path))
        coords, classes, difficult = cls.parse_annotation_file(ann_path)
        scaled_image = cv2.resize(image, (0, 0), fx=scale, fy=scale)

        image_tiles, total_boxes, total_classes, total_difficult = cls.chip_image(
            scaled_image,
            coords * scale,
            classes,
            difficult,
            tile_size=(tile_size, tile_size),
            tile_step=(tile_step, tile_step),
            min_visibility=min_visibility,
            min_area=min_area,
        )
        num_tiles = len(image_tiles)

        for i in range(num_tiles):
            tile_image = image_tiles[i]
            tile_boxes = total_boxes[i]
            tile_classes = total_classes[i]
            tile_difficult = total_difficult[i]

            tile_image_path = output_images_dir / f"{ann_path.stem}_{scale:.3f}_{i:06d}.png"
            tile_label_path = output_ann_dir / f"{ann_path.stem}_{scale:.3f}_{i:06d}.txt"

            with tile_label_path.open("w") as f:
                for poly, category, diff in zip(tile_boxes, tile_classes, tile_difficult):
                    f.write(
                        f"{poly[0, 0]:.2f} {poly[0, 1]:.2f} {poly[1, 0]:.2f} {poly[1, 1]:.2f} {poly[2, 0]:.2f} {poly[2, 1]:.2f} {poly[3, 0]:.2f} {poly[3, 1]:.2f} {category} {diff}\n"  # noqa
                    )

            cv2.imwrite(str(tile_image_path), tile_image)
