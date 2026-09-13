"""Service catalogue + sample providers so the app is usable before real data is loaded.

Sample providers are flagged ``is_demo`` and shown with a "Sample data" badge. Their prices are
indicative private-sector ranges for illustration only, and their reviews are sample text — not
Google reviews. Delete them from Admin → Dashboard once real providers are added (import a price
sheet, or use `python manage.py discover "private hospitals in Cape Town"` to pull real facilities,
ratings and reviews from Google).
"""

import random

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Provider, Review, Service
from .services.pricing import get_or_create_service, upsert_listing
from .utils import unique_slug

# name, category, keywords, indicative ZAR range (private sector)
CATALOGUE = [
    ("GP consultation", "Primary Care", "gp, doctor, general practitioner, consult, check-up, sick, flu, fever", 450, 950),
    ("Specialist consultation", "Specialist", "specialist, physician, referral", 950, 2400),
    ("Travel clinic consultation", "Travel Health", "travel clinic, travel advice, pre-travel", 380, 800),
    ("Yellow fever vaccination", "Travel Health", "yellow fever, vaccine, jab, travel vaccine, certificate", 480, 900),
    ("Emergency room visit", "Emergency", "emergency, casualty, er, trauma unit, accident, urgent", 1400, 3800),
    ("Wound suturing", "Emergency", "stitches, sutures, cut, laceration, wound", 950, 2600),
    ("Dental check-up & cleaning", "Dental", "dental check up, dental exam, scale and polish, teeth cleaning, cleaning", 650, 1450),
    ("Tooth extraction", "Dental", "extraction, pull tooth, tooth removal, wisdom tooth", 750, 2800),
    ("Root canal treatment", "Dental", "root canal, endodontic, tooth nerve, toothache, tooth pain", 4200, 9800),
    ("Dental crown", "Dental", "crown, cap, porcelain crown", 5500, 12500),
    ("Teeth whitening", "Dental", "whitening, bleaching, white teeth", 2500, 6500),
    ("Chest X-ray", "Imaging", "x-ray, xray, chest xray, radiograph", 480, 1150),
    ("Ultrasound scan", "Imaging", "ultrasound, sonar, sonogram, pregnancy scan", 950, 2300),
    ("CT scan", "Imaging", "ct, cat scan, computed tomography", 3800, 9500),
    ("MRI scan", "Imaging", "mri, magnetic resonance", 6500, 15000),
    ("Full blood count", "Pathology", "blood test, fbc, blood count, bloods", 250, 650),
    ("Malaria test", "Pathology", "malaria, malaria rapid test, parasite", 280, 700),
    ("Physiotherapy session", "Rehabilitation", "physio, physiotherapy, sports injury, back pain", 550, 1050),
    ("Eye test", "Eye Care", "eye test, optometrist, vision test, glasses", 350, 780),
    ("Cataract surgery (per eye)", "Eye Care", "cataract, lens replacement, eye surgery", 22000, 42000),
    ("Appendectomy", "Surgery", "appendix, appendicitis, appendectomy", 45000, 98000),
    ("Knee arthroscopy", "Surgery", "knee scope, arthroscopy, meniscus, knee surgery", 38000, 82000),
    ("Hip replacement", "Surgery", "hip replacement, hip arthroplasty, hip surgery", 130000, 230000),
    ("Hernia repair", "Surgery", "hernia, inguinal hernia", 32000, 70000),
    ("Natural birth", "Maternity", "normal delivery, vaginal birth, giving birth, labour, maternity", 35000, 62000),
    ("Caesarean section", "Maternity", "c-section, caesarean, cesarean", 55000, 95000),
]

# name, type, city, province, lat, lng, price multiplier, avg wait, service categories, partner
DEMO_PROVIDERS = [
    ("Rosebank Sample Day Clinic", "Clinic", "Johannesburg", "Gauteng", -26.1467, 28.0436, 0.9, 25,
     ["Primary Care", "Travel Health", "Pathology", "Emergency"], True),
    ("Highveld Sample Private Hospital", "Hospital", "Johannesburg", "Gauteng", -26.1076, 28.0567, 1.15, 55,
     ["Emergency", "Imaging", "Surgery", "Maternity", "Specialist", "Pathology"], True),
    ("Braamfontein Sample Dental Studio", "Dental Practice", "Johannesburg", "Gauteng", -26.1929, 28.0305, 0.95, 15,
     ["Dental"], False),
    ("Atlantic Sample Medical Centre", "Clinic", "Cape Town", "Western Cape", -33.9180, 18.4233, 1.0, 20,
     ["Primary Care", "Travel Health", "Emergency", "Pathology", "Rehabilitation"], True),
    ("Table Bay Sample Hospital", "Hospital", "Cape Town", "Western Cape", -33.9321, 18.4390, 1.2, 60,
     ["Emergency", "Imaging", "Surgery", "Maternity", "Specialist", "Eye Care"], True),
    ("Gardens Sample Smile Clinic", "Dental Practice", "Cape Town", "Western Cape", -33.9345, 18.4105, 1.05, 10,
     ["Dental"], False),
    ("Umhlanga Sample Medical Centre", "Clinic", "Durban", "KwaZulu-Natal", -29.7255, 31.0850, 0.95, 30,
     ["Primary Care", "Travel Health", "Pathology", "Rehabilitation"], False),
    ("Berea Sample Private Hospital", "Hospital", "Durban", "KwaZulu-Natal", -29.8420, 31.0050, 1.05, 45,
     ["Emergency", "Imaging", "Surgery", "Maternity", "Specialist"], True),
    ("Hatfield Sample Family Practice", "GP Practice", "Pretoria", "Gauteng", -25.7480, 28.2380, 0.85, 20,
     ["Primary Care", "Pathology", "Travel Health"], False),
    ("Menlyn Sample Imaging & Radiology", "Radiology", "Pretoria", "Gauteng", -25.7830, 28.2750, 0.9, 35,
     ["Imaging"], True),
    ("Summerstrand Sample Day Hospital", "Day Hospital", "Gqeberha", "Eastern Cape", -33.9900, 25.6700, 0.88, 40,
     ["Emergency", "Surgery", "Eye Care", "Primary Care"], False),
    ("Westdene Sample Eye & Physio Centre", "Specialist Practice", "Bloemfontein", "Free State", -29.1060, 26.2010, 0.8, 25,
     ["Eye Care", "Rehabilitation"], False),
]

SAMPLE_REVIEWS = [
    (5, "Staff explained the costs upfront and the final bill matched the estimate."),
    (4, "Clean facility and friendly nurses. Waited a little longer than expected."),
    (5, "Quick service, I was in and out within the hour."),
    (3, "Good care, but parking was difficult and reception was busy."),
    (4, "Doctor was thorough and took time to answer questions about my travel plans."),
    (5, "Very professional. Payment options were clear before treatment."),
]


def seed_catalogue(db: Session) -> None:
    for name, category, keywords, *_ in CATALOGUE:
        service, _ = get_or_create_service(db, name, category)
        service.category = category
        if not service.keywords:
            service.keywords = keywords
    db.commit()


def seed_demo(db: Session) -> bool:
    """Seed sample providers only on an empty providers table."""
    if db.scalar(select(func.count(Provider.id))):
        return False
    seed_catalogue(db)
    rng = random.Random(2026)
    services = {s.name: s for s in db.scalars(select(Service))}
    for (name, ptype, city, province, lat, lng, mult, wait, categories, partner) in DEMO_PROVIDERS:
        provider = Provider(
            name=name,
            slug=unique_slug(db, Provider, name),
            provider_type=ptype,
            description=f"Sample {ptype.lower()} in {city} used to demonstrate CostCare. Replace with real providers.",
            specialties=", ".join(categories),
            address=f"{rng.randint(1, 180)} Sample Road, {city}",
            city=city,
            province=province,
            latitude=lat + rng.uniform(-0.01, 0.01),
            longitude=lng + rng.uniform(-0.01, 0.01),
            phone=f"0{rng.randint(10, 87)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}",
            avg_wait_minutes=wait,
            opening_hours="Monday–Friday: 07:00–19:00\nSaturday: 08:00–13:00\nSunday: Closed"
            if ptype not in {"Hospital"} else "Open 24 hours",
            is_partner=partner,
            is_verified=partner,
            is_demo=True,
        )
        db.add(provider)
        db.flush()
        for sname, category, _, lo, hi in CATALOGUE:
            if category not in categories:
                continue
            factor = mult * rng.uniform(0.92, 1.08)
            price_min = round(lo * factor / 10) * 10
            price_max = max(price_min, round(hi * factor / 10) * 10)
            upsert_listing(db, provider, services[sname], price_min, price_max, source="demo",
                           notes="Indicative sample range — confirm with provider.")
        for rating, text in rng.sample(SAMPLE_REVIEWS, 3):
            provider.reviews.append(Review(source="demo", author_name="Sample reviewer", rating=rating, text=text,
                                           relative_time=f"{rng.randint(1, 11)} months ago"))
    db.commit()
    return True
