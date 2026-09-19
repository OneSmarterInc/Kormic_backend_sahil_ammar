"""Normalize manually supplied numbers, without implying ownership verification."""
import re
import phonenumbers
from rest_framework import serializers

REGIONS = {'India': 'IN', 'United States': 'US', 'Canada': 'CA', 'United Kingdom': 'GB', 'Australia': 'AU', 'Germany': 'DE', 'Ireland': 'IE', 'United Arab Emirates': 'AE', 'Singapore': 'SG', 'New Zealand': 'NZ', 'France': 'FR', 'Italy': 'IT', 'Spain': 'ES', 'Netherlands': 'NL', 'Switzerland': 'CH', 'Sweden': 'SE', 'Norway': 'NO', 'Denmark': 'DK', 'Finland': 'FI', 'Japan': 'JP', 'South Korea': 'KR', 'China': 'CN', 'Hong Kong': 'HK', 'Malaysia': 'MY', 'Thailand': 'TH', 'Indonesia': 'ID', 'Vietnam': 'VN', 'Philippines': 'PH', 'Brazil': 'BR', 'Mexico': 'MX', 'Argentina': 'AR', 'South Africa': 'ZA', 'Egypt': 'EG', 'Nigeria': 'NG', 'Bangladesh': 'BD', 'Sri Lanka': 'LK', 'Nepal': 'NP', 'Bhutan': 'BT'}

def normalize_phone(value, country=""):
    text = value.strip()
    if not text:
        return ""
    message = "Enter a valid phone number with country code, or select its country."
    if len(text) > 40 or not re.fullmatch(r"\+?[\d\s().-]+", text):
        raise serializers.ValidationError(message)
    country = country.strip()
    region = REGIONS.get(country) or (country.upper() if country.upper() in phonenumbers.SUPPORTED_REGIONS else None)
    try:
        number = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        raise serializers.ValidationError(message)
    if number.extension or not phonenumbers.is_valid_number(number):
        raise serializers.ValidationError(message)
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
