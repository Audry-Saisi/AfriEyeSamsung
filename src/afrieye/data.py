"""Load DeepDRiD labels. Official splits keep each patient in exactly one split."""
from pathlib import Path
import pandas as pd

ROOT = "regular_fundus_images"


def load_labels(repo) -> pd.DataFrame:
    repo = Path(repo) / ROOT
    rows = []
    for split, sub in [("train", "regular-fundus-training"), ("val", "regular-fundus-validation")]:
        d = pd.read_csv(repo / sub / f"{sub}.csv")
        d["grade"] = d["left_eye_DR_Level"].fillna(d["right_eye_DR_Level"])
        d["quality"] = d["Overall quality"]
        d["split"] = split
        d["path"] = [str(repo / sub / "Images" / str(p) / f"{i}.jpg") for p, i in zip(d.patient_id, d.image_id)]
        rows.append(d[["patient_id", "image_id", "grade", "quality", "split", "path"]])
    ev = repo / "Online-Challenge1&2-Evaluation"
    g = pd.read_excel(ev / "Challenge1_labels.xlsx").rename(columns={"DR_Levels": "grade"})
    q = pd.read_excel(ev / "Challenge2_labels.xlsx").rename(columns={"Overall quality": "quality"})
    t = g.merge(q[["image_id", "quality"]], on="image_id")
    t["patient_id"] = t.image_id.str.split("_").str[0].astype(int)
    t["split"] = "test"
    t["path"] = [str(ev / "Images" / str(p) / f"{i}.jpg") for p, i in zip(t.patient_id, t.image_id)]
    d = pd.concat(rows + [t[["patient_id", "image_id", "grade", "quality", "split", "path"]]], ignore_index=True)
    d["eye"] = d.patient_id.astype(str) + "_" + d.image_id.str.split("_").str[1].str[0]
    return d
