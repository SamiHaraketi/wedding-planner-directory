"""Style tagging from a business's own name / website text. Multilingual keyword lists.
Only tags that are actually evidenced in the text are returned (never guessed)."""
import re

STYLES = {
    "rustic": ["rustic", "barn", "farmhouse", "farm", "rústic", "quinta", "finca", "ferme", "scheune",
               "cascina", "country", "campestre"],
    "boho": ["boho", "bohemian", "bohème", "boheme", "bohemio"],
    "modern": ["modern", "contemporary", "minimalist", "design hotel", "moderno", "moderne", "contemporâneo"],
    "classic": ["classic", "elegant", "ballroom", "palace", "palácio", "palacio", "palazzo", "chateau",
                "château", "castle", "castelo", "castillo", "castello", "schloss", "manor", "historic",
                "clássico", "historic"],
    "beach": ["beach", "seaside", "oceanfront", "playa", "praia", "plage", "strand", "spiaggia", "sea view"],
    "vineyard": ["vineyard", "winery", "wine estate", "vigneto", "bodega", "vinha", "domaine", "weingut",
                 "vignoble"],
    "garden": ["garden", "jardim", "jardín", "jardin", "giardino", "garten", "orchard", "greenhouse"],
    "luxury": ["luxury", "luxurious", "5-star", "five-star", "exclusive", "boutique", "lujo", "luxo", "luxe",
               "prestige", "bespoke"],
    "romantic": ["romantic", "romántico", "romântico", "romantique", "romantico"],
    "urban": ["loft", "rooftop", "industrial", "warehouse", "urban", "skyline", "terraço", "azotea"],
    "intimate": ["intimate", "elopement", "micro wedding", "small wedding", "íntimo", "intimo"],
    "destination": ["destination wedding", "destination", "resort"],
}
STYLE_NAMES = sorted(STYLES)
_RX = {k: re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in v) + r")(?!\w)", re.I)
       for k, v in STYLES.items()}


def tag_styles(text, top=4, min_count=2):
    """Return up to `top` style tags whose keywords appear at least `min_count` times."""
    counts = {k: len(rx.findall(text or "")) for k, rx in _RX.items()}
    ranked = sorted((c, k) for k, c in counts.items() if c >= min_count)
    return [k for c, k in reversed(ranked)][:top]
