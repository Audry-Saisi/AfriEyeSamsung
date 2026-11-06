"""Demo list of areas and hospitals. Coordinates are approximate.

Hospital names are examples for this research demo. Before real use, confirm which ones offer
eye services and that they agree to take part.
"""
from math import asin, cos, radians, sin, sqrt

AREAS = [
    ("korogocho", "Korogocho", "Nairobi", -1.2513, 36.8856), ("mathare", "Mathare", "Nairobi", -1.2600, 36.8580),
    ("kibera", "Kibera", "Nairobi", -1.3133, 36.7878), ("kawangware", "Kawangware", "Nairobi", -1.2800, 36.7500),
    ("kikuyu", "Kikuyu", "Kiambu", -1.2463, 36.6629), ("thika", "Thika", "Kiambu", -1.0333, 37.0693),
    ("kitale", "Kitale", "Trans Nzoia", 1.0157, 35.0062), ("kiminini", "Kiminini", "Trans Nzoia", 0.8960, 34.9200),
    ("eldoret", "Eldoret", "Uasin Gishu", 0.5143, 35.2698), ("kisumu", "Kisumu", "Kisumu", -0.0917, 34.7680),
    ("vihiga", "Vihiga", "Vihiga", 0.0760, 34.7230), ("nakuru", "Nakuru", "Nakuru", -0.3031, 36.0800),
    ("kerugoya", "Kerugoya", "Kirinyaga", -0.4989, 37.2803), ("mombasa", "Mombasa", "Mombasa", -4.0435, 39.6682),
]
HOSPITALS = [
    ("knh", "Kenyatta National Hospital", "Nairobi", -1.3007, 36.8073), ("lions", "Lions SightFirst Eye Hospital", "Nairobi", -1.2553, 36.7533),
    ("kikuyu_eye", "PCEA Kikuyu Hospital Eye Unit", "Kiambu", -1.2520, 36.6680), ("kitale_crh", "Kitale County Referral Hospital", "Trans Nzoia", 1.0187, 35.0020),
    ("mtrh", "Moi Teaching and Referral Hospital", "Uasin Gishu", 0.5090, 35.2800), ("jootrh", "Jaramogi Oginga Odinga Hospital", "Kisumu", -0.0889, 34.7700),
    ("sabatia", "Sabatia Eye Hospital", "Vihiga", 0.1280, 34.7950), ("nakuru_crh", "Nakuru County Referral Hospital", "Nakuru", -0.2880, 36.0700),
    ("kerugoya_crh", "Kerugoya County Referral Hospital", "Kirinyaga", -0.4980, 37.2800), ("cgh", "Coast General Hospital", "Mombasa", -4.0600, 39.6700),
]
AREA = {a[0]: dict(id=a[0], name=a[1], county=a[2], lat=a[3], lng=a[4]) for a in AREAS}
HOSPITAL = {h[0]: dict(id=h[0], name=h[1], county=h[2], lat=h[3], lng=h[4]) for h in HOSPITALS}


def km(a, b):
    """Straight-line distance between two places, in kilometres."""
    la, lb, dlat, dlng = radians(a["lat"]), radians(b["lat"]), radians(b["lat"] - a["lat"]), radians(b["lng"] - a["lng"])
    return 6371 * 2 * asin(sqrt(sin(dlat / 2) ** 2 + cos(la) * cos(lb) * sin(dlng / 2) ** 2))
