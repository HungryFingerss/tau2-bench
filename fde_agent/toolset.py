"""The 7 tools the model can call, in one place.

The driver looks tools up here by name, and the model client (piece 2) will
read their NAME, DESCRIPTION and PARAMETERS from here to tell the model about them.
"""

from fde_agent.tools import (
    confirm_change,
    find_replacement,
    find_variants,
    get_order,
    prepare_change,
    transfer_to_human,
    verify_customer,
)

MODULES = (
    verify_customer,
    get_order,
    find_variants,
    find_replacement,
    prepare_change,
    confirm_change,
    transfer_to_human,
)

# name -> the tool function, e.g. "get_order" -> get_order.get_order
TOOLS = {m.NAME: getattr(m, m.NAME) for m in MODULES}
