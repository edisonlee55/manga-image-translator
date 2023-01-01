import os
import shutil
import numpy as np
import torch
import cv2
import einops
from typing import Tuple

from utils import Quadrilateral, det_rearrange_forward
from .ctd_utils import TextBlock
from .default_utils.DBNet_resnet34 import TextDetection as TextDetectionDefault
from .default_utils import imgproc, dbnet_utils, craft_utils
from .common import OfflineDetector

MODEL = None
def det_batch_forward_default(batch: np.ndarray, device: str) -> Tuple[np.ndarray, np.ndarray]:
    global MODEL
    if isinstance(batch, list):
        batch = np.array(batch)
    batch = einops.rearrange(batch.astype(np.float32) / 127.5 - 1.0, 'n h w c -> n c h w')
    batch = torch.from_numpy(batch).to(device)
    with torch.no_grad():
        db, mask = MODEL(batch)
        db = db.sigmoid().cpu().numpy()
        mask = mask.cpu().numpy()
    return db, mask

class DefaultDetector(OfflineDetector):
    _MODEL_MAPPING = {
        'model': {
            'url': 'https://github.com/zyddnys/manga-image-translator/releases/download/beta-0.3/detect.ckpt',
            'hash': '69080aea78de0803092bc6b751ae283ca463011de5f07e1d20e6491b05571a30',
            'file': '.',
        }
    }

    def __init__(self, *args, **kwargs):
        os.makedirs(self._MODEL_DIR, exist_ok=True)
        if os.path.exists('detect.ckpt'):
            shutil.move('detect.ckpt', self._get_file_path('detect.ckpt'))
        super().__init__(*args, **kwargs)

    async def _load(self, device: str):
        self.model = TextDetectionDefault()
        sd = torch.load(self._get_file_path('detect.ckpt'), map_location='cpu')
        self.model.load_state_dict(sd['model'] if 'model' in sd else sd)
        self.model.eval()
        self.device = device
        if device == 'cuda':
            self.model = self.model.cuda()
        global MODEL
        MODEL = self.model

    async def _unload(self):
        del self.model

    async def _forward(self, image: np.ndarray, detect_size: int, text_threshold: float, box_threshold: float,
                       unclip_ratio: float, det_rearrange_max_batches: int, verbose: bool = False) -> tuple[list[TextBlock], np.ndarray]:

        # TODO: Move det_rearrange_forward to common.py and try to refactor
        db, mask = det_rearrange_forward(image, det_batch_forward_default, detect_size, det_rearrange_max_batches, device=self.device, verbose=verbose)

        if db is None:
            # rearrangement is not required, fallback to default forward
            img_resized, target_ratio, _, pad_w, pad_h = imgproc.resize_aspect_ratio(cv2.bilateralFilter(image, 17, 80, 80), detect_size, cv2.INTER_LINEAR, mag_ratio = 1)
            img_resized_h, img_resized_w = img_resized.shape[:2]
            ratio_h = ratio_w = 1 / target_ratio
            if verbose:
                print(f'Detection resolution: {img_resized_w}x{img_resized_h}')
            db, mask = det_batch_forward_default([img_resized], self.device)
        else:
            img_resized_h, img_resized_w = image.shape[:2]
            ratio_w = ratio_h = 1
            pad_h = pad_w = 0

        mask = mask[0, 0, :, :]
        det = dbnet_utils.SegDetectorRepresenter(text_threshold, box_threshold, unclip_ratio=unclip_ratio)
        # boxes, scores = det({'shape': [(img_resized.shape[0], img_resized.shape[1])]}, db)
        boxes, scores = det({'shape':[(img_resized_h, img_resized_w)]}, db)
        boxes, scores = boxes[0], scores[0]
        if boxes.size == 0:
            polys = []
        else:
            idx = boxes.reshape(boxes.shape[0], -1).sum(axis=1) > 0
            polys, _ = boxes[idx], scores[idx]
            polys = polys.astype(np.float64)
            polys = craft_utils.adjustResultCoordinates(polys, ratio_w, ratio_h, ratio_net=1)
            polys = polys.astype(np.int16)
        textlines = [Quadrilateral(pts.astype(int), '', 0) for pts in polys]
        textlines = list(filter(lambda q: q.area > 16, textlines))
        mask_resized = cv2.resize(mask, (mask.shape[1] * 2, mask.shape[0] * 2), interpolation=cv2.INTER_LINEAR)
        if pad_h > 0:
            mask_resized = mask_resized[:-pad_h, :]
        elif pad_w > 0:
            mask_resized = mask_resized[:, :-pad_w]
        raw_mask = np.clip(mask_resized * 255, 0, 255).astype(np.uint8)

        if verbose:
            img_bbox_raw = np.copy(image)
            for txtln in textlines:
                cv2.polylines(img_bbox_raw, [txtln.pts], True, color=(255, 0, 0), thickness=2)
            cv2.imwrite(f'result/bboxes_unfiltered.png', cv2.cvtColor(img_bbox_raw, cv2.COLOR_RGB2BGR))
            cv2.imwrite(f'result/mask_raw.png', raw_mask)


        # text_regions = await self._merge_textlines(textlines, image.shape[1], image.shape[0])
        # final_mask = await self._refine_textmask(textlines, image, raw_mask)

        return textlines, raw_mask

        # if verbose:
        #     img_bbox = np.copy(image)
        #     print('VERBOSE', text_regions)
        #     for region in text_regions:
        #         print('REGION', region.pts)
        #         for txtln in region.textlines:
        #             print('TXTLN', txtln.pts)
        #             cv2.polylines(img_bbox, [txtln.pts], True, color=(255, 0, 0), thickness=2)
        #         img_bbox = cv2.polylines(img_bbox, [region.pts], True, color=(0, 0, 255), thickness=2)
        #     cv2.imwrite(f'result/bboxes.png', cv2.cvtColor(img_bbox, cv2.COLOR_RGB2BGR))

        # return text_regions, final_mask
