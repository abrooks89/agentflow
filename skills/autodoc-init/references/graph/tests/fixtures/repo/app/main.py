"""Fixture: imports, calls, routes."""
from app.service import compute, Service
import app.util as u


@router.get("/items")
def list_items():
    return compute()


def top():
    u.util_fn()
    return Service


def local_helper():
    return top()


unknown_call()
