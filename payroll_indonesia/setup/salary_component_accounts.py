# payroll_indonesia/setup/salary_component_accounts.py
"""
Auto-set account mapping untuk Salary Component per company.
Dijalankan setiap after_migrate supaya tidak hilang saat deploy.

Entry di ACCOUNT_MAPPING boleh berupa:
- string literal nama Account persis, contoh "Salary Expense - TPT"
  (dipakai apa adanya, cocok untuk CoA lama yang tidak pakai account_number), atau
- tuple (account_number, label), contoh ("6211001", "SALARIES")
  (di-resolve dinamis lewat account_number + company, jadi TIDAK perlu hardcode
  abbr company — otomatis cocok baik di site yang abbr-nya PPR maupun PPO,
  selama Account dengan account_number itu memang ada di CoA company tsb).
"""

import frappe


ACCOUNT_MAPPING = {
    "Tiga Perkasa Teknik": {
        "BPJS Kesehatan Employee": "2131011 - Accrued Expense Health Insurance/BPJS - TPT",
        "BPJS JHT Employee": "2131014 - Accrued Payable BPJS JHT - TPT",
        "BPJS JP Employee": "2131015 - Accrued Payable BPJS JP - TPT",
    },
    "Pemuda Patriot R": {
        "Gaji Pokok": ("6211001", "SALARIES"),
        "Basic Salary": ("6211001", "SALARIES"),
        "Tunjangan Transport": ("6211005", "TRANSPORTATION"),
        "Tunjangan Operational": ("6211004", "OPERATIONAL"),
        "Tunjangan Makan": ("6211007", "MEAL"),
        "Tunjangan Pajak atas Insentif": ("6211002", "ALLOWANCE"),
        "Insentif Penjualan": ("6211015", "INCENTIVE"),
        "Kendaraan Dinas": ("6211023", "LAIN LAIN"),
        "Asuransi Tambahan": ("6211010", "LIFE INSURANCE"),
        "Bingkisan Hari Raya": ("6211018", "T.H.R & BONUS"),
        "Seragam Kerja": ("6241001", "UNIFORM"),
        "Makan di Kantor": ("6221002", "CAFETARIA"),
        "Bonus": ("6211017", "BONUS"),
        "THR": ("6211018", "T.H.R & BONUS"),
    },
}


def _resolve_account(company: str, account_ref):
    """Balikin nama Account (name) yang beneran ada di site ini, atau None."""
    if isinstance(account_ref, (tuple, list)):
        account_number, label = account_ref

        account_name = frappe.db.get_value(
            "Account",
            {"company": company, "account_number": account_number},
            "name",
        )
        if account_name:
            return account_name

        # Fallback: coba tebak lewat abbr company kalau account_number belum keisi
        abbr = frappe.db.get_value("Company", company, "abbr")
        if abbr:
            guess = f"{account_number} - {label} - {abbr}"
            if frappe.db.exists("Account", guess):
                return guess
        return None

    # String literal — dipakai apa adanya
    return account_ref if frappe.db.exists("Account", account_ref) else None


def sync_salary_component_accounts():
    """Force-sync account mapping untuk semua company yang terdaftar.

    Ini bukan "isi kalau kosong" — kalau row untuk company itu sudah ada tapi
    akunnya beda dari ACCOUNT_MAPPING, akan di-update supaya dict ini tetap
    jadi satu-satunya sumber kebenaran dan gak balik lagi ke default lama tiap
    migrate/deploy.
    """
    for company, components in ACCOUNT_MAPPING.items():
        # Skip kalau company tidak ada di site ini
        if not frappe.db.exists("Company", company):
            continue

        for component_name, account_ref in components.items():
            # Skip kalau salary component tidak ada
            if not frappe.db.exists("Salary Component", component_name):
                continue

            account = _resolve_account(company, account_ref)
            if not account:
                frappe.logger("payroll_indonesia").warning(
                    f"Account untuk {component_name} ({company}) tidak ditemukan di site ini, "
                    f"skip. Ref: {account_ref}"
                )
                continue

            doc = frappe.get_doc("Salary Component", component_name)

            existing = next((a for a in doc.accounts if a.company == company), None)
            if existing:
                if existing.account == account:
                    continue  # sudah benar, tidak perlu apa-apa
                existing.account = account
                action = "Updated"
            else:
                doc.append("accounts", {
                    "company": company,
                    "account": account
                })
                action = "Added"

            doc.flags.ignore_links = True
            doc.flags.ignore_permissions = True
            doc.save()
            print(f"✓ {action}: {component_name} ({company}) → {account}")

    frappe.db.commit()
