import json


class OrderError(Exception):
    """Base class for order failures."""


class OrderNotFound(OrderError):
    """The order does not exist."""


def read_orders(path):
    """Load every order from a JSON file."""
    with open(path) as handle:
        return json.load(handle)


def find_order(path, order_id):
    """Return one order.

    Raises
    ------
    OrderNotFound
        If no order has this identifier.
    """
    for order in read_orders(path):
        if order["id"] == order_id:
            return order
    raise OrderNotFound(order_id)


def order_total(path, order_id):
    """Return the total of an order, or zero when it does not exist.

    :raises PermissionError: if the file is not readable
    """
    try:
        return find_order(path, order_id)["total"]
    except OrderError:
        return 0
