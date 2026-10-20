'''Utility functions for data normalization.

This module provides helper functions to clean and standardize
customer data structures used across the project. The primary
focus is on handling non‑FinAgg customers where the incoming payload
uses the key 'companyName' but the rest of the codebase expects the
canonical key 'orgName'.
'''

from typing import Any


def normalize_customer_data(customer: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of a customer dict.

    * If the ``customer`` dict contains a ``'companyName'`` key and does
      not already have an ``'orgName'`` key, the value is moved to
      ``'orgName'``.
    * The original ``'companyName'`` key is removed to avoid duplicate
      data.
    * All other keys are left untouched.
    * The function works for both FinAgg and non‑FinAgg customers –
      FinAgg customers typically already have ``'orgName'`` and will be
      returned unchanged.

    Parameters
    ----------
    customer:
        Dictionary representing a customer record.

    Returns
    -------
    dict
        A new dictionary with the normalized ``orgName`` field.
    """
    # Work on a shallow copy to avoid mutating the original object.
    normalized = dict(customer)

    # Detect non‑FinAgg case: ``companyName`` present and ``orgName`` missing.
    if 'companyName' in normalized and 'orgName' not in normalized:
        normalized['orgName'] = normalized.pop('companyName')
    # If both keys exist, prefer ``orgName`` and drop the duplicate.
    elif 'companyName' in normalized and 'orgName' in normalized:
        normalized.pop('companyName')

    return normalized

# Example usage (for manual testing/debugging)
if __name__ == "__main__":
    example = {
        "id": 123,
        "companyName": "Acme Corp",
        "type": "nonFinAgg",
    }
    print(normalize_customer_data(example))
