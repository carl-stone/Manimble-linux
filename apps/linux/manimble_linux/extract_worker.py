"""SAM 2.1 CPU worker. No Qt or main-environment imports on this boundary."""
from __future__ import annotations

from pathlib import Path
import math

from .extract_runtime import CHECKPOINT, verify_checkpoint


def run(job, progress):
    # Hash immediately before build_sam2 deserializes. The pinned upstream loader
    # uses torch.load(..., map_location='cpu', weights_only=True); do not override it.
    checkpoint = verify_checkpoint(job["checkpoint"])
    import numpy as np
    import torch
    from PIL import Image
    from sam2.build_sam import build_sam2
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    progress(0.05,"Loading SAM 2.1 Hiera Tiny on CPU")
    model = build_sam2(CHECKPOINT["config"],str(checkpoint),device="cpu",mode="eval",apply_postprocessing=False)
    model = model.float()
    image = np.asarray(Image.open(job["image"]).convert("RGB"))
    h,w = image.shape[:2]
    directory = Path(job["directory"])
    directory.mkdir(parents=True,exist_ok=True)
    masks = []
    with torch.inference_mode():
        if job.get("box") is None:
            generator = SAM2AutomaticMaskGenerator(
                model,points_per_side=32,points_per_batch=8,pred_iou_thresh=0.8,
                stability_score_thresh=0.95,box_nms_thresh=0.7,crop_n_layers=0,
                min_mask_region_area=0,output_mode="binary_mask",
                max_hole_area=0,max_sprinkle_area=0,
            )
            progress(0.15,"Finding editable segment candidates (CPU)")
            masks = [candidate["segmentation"] for candidate in generator.generate(image)]
        else:
            x0,y0,x1,y1 = map(float,job["box"])
            x0,y0,x1,y1 = max(0,x0),max(0,y0),min(w,x1),min(h,y1)
            if x1 <= x0 or y1 <= y0:
                raise ValueError("The extraction box is outside the picture")
            predictor = SAM2ImagePredictor(model,max_hole_area=0,max_sprinkle_area=0)
            predictor.set_image(image)
            progress(0.3,"Finding the element inside the box")
            predicted,_,_ = predictor.predict(box=np.array([x0,y0,x1,y1],dtype=np.float32),multimask_output=False)
            clipped = np.zeros((h,w),np.bool_)
            ys,xs = slice(math.floor(y0),math.ceil(y1)),slice(math.floor(x0),math.ceil(x1))
            clipped[ys,xs] = predicted[0,ys,xs] > 0
            if clipped.any():
                masks = [clipped]
    files = []
    for i,mask in enumerate(masks):
        path = directory / f"mask-{i}.png"
        temporary = path.with_suffix(".tmp.png")
        Image.fromarray(np.asarray(mask,dtype=np.uint8)*255).save(temporary)
        temporary.replace(path)
        files.append(str(path))
    progress(1,"Candidates ready" if files else "No separate elements found; try Whole Box or Keep")
    return {"masks":files,"count":len(files)}


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(run)
