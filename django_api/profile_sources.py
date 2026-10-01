"""Combine complementary source facts without case-sensitive duplicates."""
def merge_skills(*groups):
    result, seen = [], set()
    for group in groups:
        for value in group if isinstance(group, list) else []:
            if not isinstance(value, str):
                continue
            value = value.strip()
            key = ' '.join(value.casefold().split())
            if key and key not in seen:
                seen.add(key)
                result.append(value)
    return result
