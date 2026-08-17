"""Bluetooth LE backend package."""

# Temporary diagnostic instrumentation for diag/esphome-cccd-status133.
# It wraps aioesphomeapi calls only for logging/timing and does not alter their
# arguments, order, retries, or return values.
from .GattDiag import install as _install_gatt_diag

_install_gatt_diag()
del _install_gatt_diag
