"""Name comparison for CV ownership; never compare filenames or document prose."""
import unicodedata


IGNORED_WORDS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "student", "unknown", "null", "none"})


def name_words(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    normalized = unicodedata.normalize("NFKD", value.casefold())
    # Preserve letters from every script; ignore accents, punctuation and initials.
    letters = "".join(
        char if char.isalpha() else " "
        for char in normalized if not unicodedata.combining(char)
    )
    return {word for word in letters.split() if len(word) > 1 and word not in IGNORED_WORDS}


def validate_resume_name(registered_name: str, extracted_name: object) -> None:
    expected = name_words(registered_name)
    actual = name_words(extracted_name)
    if not expected:
        raise ValueError("Add your full name to your profile before uploading a CV.")
    if not actual:
        raise ValueError("We could not read your name from this CV. Upload a clearer CV with your full name.")
    if not expected.intersection(actual):
        raise ValueError("The name on this CV does not match your student name. Upload your own CV with at least one matching name word.")
