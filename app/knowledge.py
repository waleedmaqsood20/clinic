"""
Clinic profile and FAQ data — the only file you normally edit.

Edit CLINIC_PROFILE and FAQS to match your clinic. Restart the server after changes.
"""
from __future__ import annotations
from dataclasses import dataclass

CLINIC_PROFILE = {
    "name": "Bright Smile Dental & Aesthetics",
    "timezone": "America/Indiana/Indianapolis",
    "hours": "Monday through Friday, 8am to 5pm, closed weekends",
    "address": "8200 North Meridian Street, Indianapolis, Indiana 46260",
    "parking": "Free patient parking in the lot right out front.",
    "phone": "(317) 555-0100",
}


@dataclass
class Doc:
    triggers: str   # space-separated keywords used for matching
    answer: str


FAQS: list[Doc] = [
    Doc("hours open close weekend saturday sunday",
        f"We're open {CLINIC_PROFILE['hours']}."),

    Doc("address location directions where find",
        f"We're at {CLINIC_PROFILE['address']}. {CLINIC_PROFILE['parking']}"),

    Doc("parking park",
        CLINIC_PROFILE["parking"]),

    Doc("price cost fee exam cleaning how much",
        "A new-patient exam is $89 and a cleaning is $120. "
        "Whitening starts at $199. We'll give you an itemised quote before any work begins."),

    Doc("insurance accept take plan",
        "We accept most major PPO dental plans, including Delta Dental, Cigna, MetLife, "
        "and Aetna. Call us with your plan details and we'll verify benefits before your visit."),

    Doc("whitening bleach teeth bright cosmetic",
        "We offer in-office whitening starting at $199 and take-home trays from $149. "
        "Results typically last 12–18 months with good home care."),

    Doc("implant implants missing tooth replacement",
        "Dental implants replace missing teeth with a permanent titanium root and crown. "
        "Pricing starts around $1,500 per implant. We offer a free implant consultation."),

    Doc("braces invisalign straighten orthodontic align",
        "We offer Invisalign clear aligners — most cases run 6 to 18 months. "
        "Come in for a complimentary Invisalign consultation."),

    Doc("emergency pain urgent broken cracked toothache",
        "We keep same-day emergency slots. Call us at " + CLINIC_PROFILE["phone"] +
        " first thing in the morning and we'll do our best to see you today."),

    Doc("cancel reschedule change appointment",
        "We ask for at least 24 hours' notice to cancel or reschedule. "
        "Call us or we can note it here."),

    Doc("new patient first visit",
        "New patients are very welcome! We'll need about 60 minutes for your first visit: "
        "a full exam, X-rays, and a cleaning if time allows. "
        "Please arrive 10 minutes early to complete your paperwork."),

    Doc("xray x-ray digital radiation",
        "We use digital X-rays, which use up to 90% less radiation than traditional film "
        "and give us a clearer picture of your teeth and bone."),

    Doc("filling cavity decay composite",
        "We use tooth-coloured composite resin fillings that match your teeth. "
        "Most fillings take 30–60 minutes."),

    Doc("crown cap cerec same day",
        "Crowns protect and restore damaged teeth. "
        "We use same-day CEREC crowns in most cases, so you only need one visit."),

    Doc("root canal nerve endodontic infected",
        "Root canals relieve pain from an infected nerve and save the tooth. "
        "Modern root canals are no more uncomfortable than a routine filling."),

    Doc("child children kid pediatric age",
        "We see patients of all ages, including children from age 3. "
        "Our team is great with nervous little ones."),

    Doc("sedation anxious nervous fear dental anxiety nitrous",
        "We offer nitrous oxide (laughing gas) and oral sedation for anxious patients. "
        "Let us know when you book and the team will walk you through the options."),

    Doc("botox filler aesthetic facial cosmetic injection",
        "Yes, we offer facial aesthetics including Botox and dermal fillers, "
        "administered by our trained clinicians. Ask about a complimentary consultation."),

    Doc("payment plan finance monthly carecredit",
        "We offer 0% interest payment plans through CareCredit for 6 and 12 months. "
        "Ask the front desk to run a quick pre-qualification — it takes about two minutes."),
]


def search(query: str, top_k: int = 1) -> list[Doc]:
    """Return the top_k FAQ entries best matching the query by trigger-word overlap."""
    q_words = set((query or "").lower().split())
    if not q_words:
        return []
    scored = []
    for doc in FAQS:
        t_words = set(doc.triggers.lower().split())
        score = len(q_words & t_words)
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:top_k]]
