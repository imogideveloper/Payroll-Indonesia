import frappe
from .setup_module import setup_payroll_settings
from .salary_components import setup_allowance_components

def after_install():
    """Run setelah app di-install"""
    print("\n" + "="*60)
    print("Setting up Payroll Indonesia...")
    print("="*60 + "\n")
    
    setup_payroll_settings()
    setup_allowance_components()
    
    print("\n" + "="*60)
    print("✓ Payroll Indonesia setup completed!")
    print("="*60 + "\n")

def after_migrate():
    """Run setelah bench migrate"""
    # Re-apply settings untuk ensure tetap correct
    setup_payroll_settings()
    setup_allowance_components()