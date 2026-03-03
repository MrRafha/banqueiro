from __future__ import annotations


def format_silver(amount: float) -> str:
    """Format silver with Brazilian thousands separator. e.g. 15000000.5 -> '15.000.000,50'"""
    if amount is None:
        return "0"
    # Format with 2 decimal places then apply PT-BR separators
    formatted = f"{amount:,.2f}"          # 15,000,000.50 (en-US)
    int_part, dec_part = formatted.split(".")
    int_part = int_part.replace(",", ".")  # 15.000.000
    result = f"{int_part},{dec_part}"      # 15.000.000,50
    # Remove trailing zeros after comma but keep at least integer display
    if result.endswith(",00"):
        result = result[:-3]
    return result


def format_pct(pct: float) -> str:
    """Format percentage. e.g. 75.333 -> '75,33%'"""
    if pct is None:
        return "0%"
    return f"{pct:.2f}%".replace(".", ",")


def parse_silver(text: str) -> float | None:
    """Parse a user-provided silver string into a float.
    Accepts: 15000000 / 15.000.000 / 15,000,000 / 15000000.50 / 15.000.000,50
    """
    try:
        # Remove spaces
        text = text.strip().replace(" ", "")
        # Detect PT-BR format: has dots as thousands and comma as decimal
        # e.g. "15.000.000,50"
        if "," in text and "." in text:
            # Assume PT-BR: dots = thousands, comma = decimal
            text = text.replace(".", "").replace(",", ".")
        elif "," in text and "." not in text:
            # Could be "15,50" (decimal) or "15,000" (thousands)
            # If the part after comma has 3 digits, treat as thousands
            parts = text.split(",")
            if len(parts) == 2 and len(parts[1]) == 3:
                text = text.replace(",", "")  # thousands separator
            else:
                text = text.replace(",", ".")  # decimal comma
        elif "." in text and "," not in text:
            # Could be "15.000.000" (thousands) or "15.50" (decimal)
            parts = text.split(".")
            if len(parts) > 2:
                # Multiple dots → thousands separator
                text = text.replace(".", "")
            # else single dot → keep as decimal
        return float(text)
    except (ValueError, AttributeError):
        return None
