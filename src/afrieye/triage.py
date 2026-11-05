"""Convert model outputs into a triage category. Same rules as web/app.js.

Macular oedema is NOT assessed: the training data has no oedema labels. Clinical sign-off
on these rules is required before any real use.
"""
from dataclasses import dataclass

URGENT, REFER, RESCREEN, RETAKE = "Urgent referral", "Refer", "Rescreen in 12 months", "Retake photo"


@dataclass
class Thresholds:
    referable: float
    urgent: float
    quality: float


def triage(p_ge2: float, p_ge3: float, p_quality: float, t: Thresholds, retakes_used: int = 0, max_retakes: int = 2):
    """Returns (category, nurse-facing message)."""
    # Positive scores act first: in the validation data, severe DR photos were often scored as
    # low quality, so a retake must never delay a referral.
    if p_ge3 >= t.urgent:
        return URGENT, "Signs of possible severe or proliferative DR. Refer urgently."
    if p_ge2 >= t.referable:
        return REFER, "Signs of possible referable DR. Refer to an eye clinic."
    if p_quality < t.quality:
        if retakes_used < max_retakes:
            return RETAKE, "Image quality is too low for a reliable result. Retake the photo."
        return REFER, "Still ungradable after retakes. Refer for an in-person eye exam."
    return RESCREEN, "No referable DR found. This is not a full eye exam, and oedema was not checked."
