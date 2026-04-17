import frappe

def setup_allowance_components():
    """Setup formula untuk Tunjangan Makan & Transport"""
    
    components = [
        {
            "salary_component": "Tunjangan Makan",
            "salary_component_abbr": "TM",
            "type": "Earning",
            "description": "Tunjangan makan per hari × jumlah hari hadir",
            "depends_on_payment_days": 0,
            "amount_based_on_formula": 1,
            "formula": "payment_days * meal_allowance",
            "is_tax_applicable": 1,
            "round_to_the_nearest_integer": 1,
            "exempted_from_income_tax": 0
        },
        {
            "salary_component": "Tunjangan Transport",
            "salary_component_abbr": "TT",
            "type": "Earning",
            "description": "Tunjangan transport per hari × jumlah hari hadir",
            "depends_on_payment_days": 0,
            "amount_based_on_formula": 1,
            "formula": "payment_days * transport_allowance",
            "is_tax_applicable": 1,
            "round_to_the_nearest_integer": 1,
            "exempted_from_income_tax": 0
        }
    ]
    
    for comp_data in components:
        # Cek apakah sudah ada
        if frappe.db.exists("Salary Component", comp_data["salary_component"]):
            # Update existing
            doc = frappe.get_doc("Salary Component", comp_data["salary_component"])
            doc.update(comp_data)
            doc.save(ignore_permissions=True)
            print(f"✓ Updated: {comp_data['salary_component']}")
        else:
            # Create new
            doc = frappe.get_doc({
                "doctype": "Salary Component",
                **comp_data
            })
            doc.insert(ignore_permissions=True)
            print(f"✓ Created: {comp_data['salary_component']}")
    
    frappe.db.commit()