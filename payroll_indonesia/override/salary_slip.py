# payroll_indonesia/override/salary_slip.py
"""
Custom Salary Slip override for Payroll Indonesia.

- TER (bulanan) & Progressive (Desember/annual correction).
- Flow Desember (sesuai arahan):
  * Jan–Nov diambil dari Annual Payroll History (APH).
  * Desember dihitung dari slip aktif (bruto, pengurang netto bulanan, biaya jabatan bulanan).
  * Tahunan = (Jan–Nov APH) + (Desember dari slip).
  * PPh Desember (koreksi) = PPh tahunan − total PPh Jan–Nov (APH).
- Selalu menulis baris "PPh 21" di deductions dan sinkron dengan UI.
"""

try:
    from hrms.payroll.doctype.salary_slip.salary_slip import SalarySlip
except ImportError:
    from frappe.model.document import Document
    SalarySlip = Document
    import frappe
    frappe.log_error(
        message="Failed to import SalarySlip from hrms.payroll. Using Document fallback.",
        title="Payroll Indonesia Import Warning",
    )

import json
import traceback
import frappe
from frappe.utils import flt, rounded
try:
    from frappe.utils import getdate
except Exception:  # pragma: no cover
    from datetime import datetime
    def getdate(value):
        return datetime.strptime(str(value), "%Y-%m-%d")

from hrms.payroll.doctype.salary_slip.salary_slip import _safe_eval

# Hitung PPh
from payroll_indonesia.config.pph21_ter import calculate_pph21_TER
from payroll_indonesia.config.pph21_ter_december import (
    calculate_pph21_december,
    sum_bruto_earnings,               # ambil bruto taxable slip ini (Desember)
    sum_pengurang_netto_bulanan,      # pengurang netto bulanan (exclude Biaya Jabatan)
    biaya_jabatan_bulanan,            # min(5% × bruto_bulan, 500.000)
)
from payroll_indonesia.override.salary_slip_cutoff_patch import apply_salary_slip_cutoff_patch

# Sinkronisasi Annual Payroll History
from payroll_indonesia.utils.sync_annual_payroll_history import (
    build_monthly_aph_row_from_salary_slip,
    get_aph_fiscal_year_from_salary_slip,
    sync_annual_payroll_history,
)
from payroll_indonesia import _patch_salary_slip_globals

logger = frappe.logger("payroll_indonesia")

apply_salary_slip_cutoff_patch()


def _replacement_assignment_applies(replacement_name, lookup_date) -> bool:
    if not replacement_name or not frappe.db.exists("Salary Structure Assignment", replacement_name):
        return False
    replacement = frappe.db.get_value(
        "Salary Structure Assignment",
        replacement_name,
        ["from_date", "end_date", "docstatus"],
        as_dict=True,
    )
    if not replacement or replacement.get("docstatus") != 1:
        return False
    if replacement.get("from_date") and getdate(replacement.from_date) > getdate(lookup_date):
        return False
    if replacement.get("end_date") and getdate(replacement.end_date) < getdate(lookup_date):
        return False
    return True


class CustomSalarySlip(SalarySlip):
    """Salary Slip override dengan logika PPh21 Indonesia."""

    def add_tax_components(self):
        """HRMS menampilkan alert ini per slip; tampilkan sekali saja per batch Payroll Entry."""
        _orig_msgprint = frappe.msgprint

        def _msgprint_once(msg, *args, **kwargs):
            if kwargs.get("alert"):
                text = frappe.as_unicode(msg).lower()
                if "tax component" in text and "salary structure" in text:
                    if getattr(frappe.local, "tax_component_msg_guard", False):
                        return
                    frappe.local.tax_component_msg_guard = True
            return _orig_msgprint(msg, *args, **kwargs)

        frappe.msgprint = _msgprint_once
        try:
            super().add_tax_components()
        finally:
            frappe.msgprint = _orig_msgprint

    # -------------------------
    # Helpers umum
    # -------------------------
    def _get_bulan_number(self, start_date=None, nama_bulan=None, end_date=None):
        """Bulan gaji: untuk pola cutoff 25–24 pakai end_date (gaji Mei = s/d 24 Mei)."""
        end_date = end_date or getattr(self, "end_date", None)
        if end_date:
            try:
                return getdate(end_date).month
            except Exception:
                logger.debug(f"Gagal parsing end_date: {end_date}")

        bulan = None
        if start_date:
            try:
                bulan = getdate(start_date).month
            except Exception:
                logger.debug(f"Gagal parsing start_date: {start_date}")
        elif getattr(self, "start_date", None):
            try:
                bulan = getdate(self.start_date).month
            except Exception:
                pass

        if not bulan and nama_bulan:
            peta = {
                "january": 1, "jan": 1, "januari": 1,
                "february": 2, "feb": 2, "februari": 2,
                "march": 3, "mar": 3, "maret": 3,
                "april": 4, "may": 5, "mei": 5,
                "june": 6, "jun": 6, "juni": 6,
                "july": 7, "jul": 7, "juli": 7,
                "august": 8, "aug": 8, "agustus": 8,
                "september": 9, "sep": 9,
                "october": 10, "oct": 10, "oktober": 10,
                "november": 11, "nov": 11,
                "december": 12, "dec": 12, "desember": 12,
            }
            bulan = peta.get(str(nama_bulan).strip().lower())

        if not bulan:
            from datetime import datetime
            bulan = datetime.now().month
        return bulan

    def get_employee_doc(self):
        if hasattr(self, "employee"):
            emp = self.employee
            if isinstance(emp, dict):
                return emp
            try:
                return frappe.get_doc("Employee", emp)
            except frappe.DoesNotExistError:
                frappe.log_error(
                    message=f"Employee '{emp}' not found for Salary Slip {self.name}",
                    title="Payroll Indonesia Missing Employee Error",
                )
                raise frappe.ValidationError(f"Employee '{emp}' not found.")
        return {}

    # -------------------------
    # Evaluasi formula
    # -------------------------
    def _get_active_salary_structure_assignment(self):
        """HRMS menyimpan SSA di _salary_structure_assignment (dict tanpa child table)."""
        name = getattr(self, "salary_structure_assignment", None)
        if not name:
            ssa_row = getattr(self, "_salary_structure_assignment", None)
            if isinstance(ssa_row, dict):
                name = ssa_row.get("name")
            elif ssa_row is not None:
                name = getattr(ssa_row, "name", None)
        return name

    def _is_cutoff_cross_month_period(self):
        """Slip gaji 25–24: start_date dan end_date jatuh di bulan/tahun berbeda."""
        if not (self.start_date and self.end_date):
            return False
        start = getdate(self.start_date)
        end = getdate(self.end_date)
        return start.month != end.month or start.year != end.year

    def _salary_month_reference_date(self):
        """Tanggal acuan bulan gaji untuk SSA, fiscal year, dll. (bukan tanggal kerja)."""
        if self._is_cutoff_cross_month_period():
            return getdate(self.end_date)
        return getdate(self.actual_start_date)

    def set_salary_structure_assignment(self):
        """
        Cari SSA aktif. Pola gaji 25–24 memakai end_date supaya SSA from_date 1 Jan
        tetap valid untuk gaji Januari.
        """
        from frappe import _
        from frappe.utils.formatters import formatdate

        lookup_date = self._salary_month_reference_date()

        assignment_rows = frappe.get_all(
            "Salary Structure Assignment",
            filters={
                "employee": self.employee,
                "salary_structure": self.salary_structure,
                "from_date": ("<=", lookup_date),
                "docstatus": 1,
            },
            fields=["*"],
            order_by="from_date desc, creation desc",
        )
        candidate = None
        for row in assignment_rows:
            if row.get("renewed_by_assignment_contract") and _replacement_assignment_applies(
                row.get("renewed_by_assignment_contract"), lookup_date
            ):
                continue
            if row.get("end_date") and getdate(row.end_date) < lookup_date:
                continue
            candidate = row
            break
        self._salary_structure_assignment = (
            candidate
            if candidate
            else None
        )

        if not self._salary_structure_assignment:
            frappe.throw(
                _(
                    "Please assign a Salary Structure for Employee {0} applicable from or before {1} first"
                ).format(
                    frappe.bold(self.employee_name),
                    frappe.bold(formatdate(lookup_date)),
                )
            )

        if self._salary_structure_assignment.get("name"):
            self.salary_structure_assignment = self._salary_structure_assignment.name

    def get_year_to_date_period(self):
        """Fiscal year mengikuti bulan gaji (end_date) untuk slip cutoff 25–24."""
        if self.payroll_period:
            return self.payroll_period.start_date, self.payroll_period.end_date

        from erpnext.accounts.utils import get_fiscal_year

        lookup_date = self._salary_month_reference_date()
        fiscal_year = get_fiscal_year(date=lookup_date, company=self.company, as_dict=1)
        return fiscal_year.year_start_date, fiscal_year.year_end_date

    def get_data_for_eval(self):
        data, default_data = super().get_data_for_eval()
        try:
            from imogi_finance.payroll.salary_structure_assignment import (
                get_assignment_formula_context,
            )

            ssa_name = self._get_active_salary_structure_assignment()
            if ssa_name:
                ctx = get_assignment_formula_context(ssa_name)
                data.update(ctx)
                default_data.update(ctx)
        except ImportError:
            pass
        return data, default_data

    def eval_condition_and_formula(self, struct_row, data):
        context = data.copy()
        context.update(_patch_salary_slip_globals())

        assignment_ctx = {}
        ssa_name = self._get_active_salary_structure_assignment()
        if ssa_name:
            try:
                from imogi_finance.payroll.salary_structure_assignment import (
                    get_assignment_formula_context,
                )

                assignment_ctx = get_assignment_formula_context(ssa_name)
                context.update(assignment_ctx)
                data.update(assignment_ctx)
            except ImportError:
                pass

        for f in ("meal_allowance", "transport_allowance", "base", "tunjangan_operational"):
            if f in context:
                continue
            v = getattr(self, f, None)
            if v is None and getattr(self, "_salary_structure_assignment", None):
                ssa_row = self._salary_structure_assignment
                v = ssa_row.get(f) if isinstance(ssa_row, dict) else getattr(ssa_row, f, None)
            if v is not None:
                context[f] = v
                data[f] = v

        component = (getattr(struct_row, "salary_component", None) or "").strip()
        if component and not (getattr(struct_row, "formula", None) or "").strip():
            key = component.lower().replace(" ", "_")
            if flt(assignment_ctx.get(key)):
                return flt(assignment_ctx[key])

        try:
            condition = getattr(struct_row, "condition", None)
            formula = getattr(struct_row, "formula", None)
            if condition and not _safe_eval(condition, self.whitelisted_globals, context):
                return 0
            if getattr(struct_row, "amount_based_on_formula", None) and formula:
                amount = flt(
                    _safe_eval(formula, self.whitelisted_globals, context),
                    struct_row.precision("amount"),
                )
                if amount:
                    data[struct_row.abbr] = amount
                return amount
        except Exception as e:
            frappe.throw(
                f"Failed evaluating formula for {getattr(struct_row, 'salary_component', 'component')}: {e}"
            )

        return super().eval_condition_and_formula(struct_row, data)

    # -------------------------
    # PPh 21 TER (bulanan)
    # -------------------------
    def calculate_income_tax(self):
        try:
            if not getattr(self, "employee", None):
                frappe.throw("Employee data is required for PPh21 calculation", title="Missing Employee")
            if not getattr(self, "company", None):
                frappe.throw("Company is required for PPh21 calculation", title="Missing Company")

            employee_doc = self.get_employee_doc()
            bulan = self._get_bulan_number(
                start_date=getattr(self, "start_date", None),
                nama_bulan=getattr(self, "bulan", None),
            )
            taxable_income = self._calculate_taxable_income()

            result = calculate_pph21_TER(
                taxable_income=taxable_income, employee=employee_doc, company=self.company, bulan=bulan
            )
            tax_amount = flt(result.get("pph21", 0.0))

            self.tax = tax_amount
            try:
                self.tax_type = "TER"
            except AttributeError:
                result["_tax_type"] = "TER"

            self.pph21_info = json.dumps(result)
            self.update_pph21_row(tax_amount)
            return tax_amount

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.log_error(
                message=f"Failed to calculate income tax (TER): {e}\n{traceback.format_exc()}",
                title=f"Payroll Indonesia TER Calculation Error - {self.name}",
            )
            raise frappe.ValidationError(f"Error in PPh21 calculation: {e}")

    # -------------------------
    # Helper: ambil YTD Jan–Nov dari APH
    # -------------------------
    def _get_ytd_from_aph(self):
        """
        Kembalikan (ytd_bruto_jan_nov, ytd_netto_jan_nov, ytd_tax_paid_jan_nov)
        yang diambil dari Annual Payroll History (monthly_details bulan < 12).
        """
        ytd_bruto = 0.0
        ytd_netto = 0.0
        ytd_tax   = 0.0

        fiscal_year = getattr(self, "fiscal_year", None)
        if not fiscal_year and getattr(self, "start_date", None):
            fiscal_year = str(getdate(self.start_date).year)
        if not fiscal_year:
            return ytd_bruto, ytd_netto, ytd_tax

        try:
            rows = frappe.get_all(
                "Annual Payroll History",
                filters={"employee": self.employee, "fiscal_year": fiscal_year},
                fields=["name"],
                limit=1,
            )
            if rows:
                hist = frappe.get_doc("Annual Payroll History", rows[0].name)
                from payroll_indonesia.utils.aph_month import normalize_bulan

                for r in hist.get("monthly_details", []) or []:
                    bln = normalize_bulan(getattr(r, "bulan", 0))
                    if bln and bln < 12:
                        ytd_bruto += flt(getattr(r, "bruto", 0))
                        # gunakan kolom netto jika tersedia; fallback: bruto - biaya_jabatan - pengurang_netto
                        r_netto = flt(getattr(r, "netto", 0))
                        if not r_netto:
                            r_netto = flt(getattr(r, "bruto", 0)) \
                                      - flt(getattr(r, "biaya_jabatan", 0)) \
                                      - flt(getattr(r, "pengurang_netto", 0))
                        ytd_netto += r_netto
                        ytd_tax   += flt(getattr(r, "pph21", 0))
        except Exception as e:
            logger.warning(f"Error fetching YTD from Annual Payroll History: {e}")

        return ytd_bruto, ytd_netto, ytd_tax

    # -------------------------
    # PPh 21 Progressive (Desember)
    # -------------------------
    def calculate_income_tax_december(self):
        """Hitung PPh21 Desember (annual correction) sesuai arahan Desember-only."""
        try:
            if not getattr(self, "employee", None):
                frappe.throw("Employee data is required for PPh21 calculation", title="Missing Employee")
            if not getattr(self, "company", None):
                frappe.throw("Company is required for PPh21 calculation", title="Missing Company")

            employee_doc = self.get_employee_doc()

            # === 1) Ambil YTD Jan–Nov dari APH ===
            ytd_bruto_jan_nov, ytd_netto_jan_nov, ytd_tax_paid_jan_nov = self._get_ytd_from_aph()

            # === 2) Ambil data Desember dari slip aktif ===
            slip_dict = self.as_dict()
            bruto_desember = sum_bruto_earnings(slip_dict)
            pengurang_netto_desember = sum_pengurang_netto_bulanan(slip_dict)
            biaya_jabatan_desember = biaya_jabatan_bulanan(bruto_desember)  # min(5% × bruto Des, 500k)

            # >>> PENTING: Baca JP+JHT (EE) bulan Desember dari deduction slip <<<
            jp_jht_employee_month = 0.0
            for d in (slip_dict.get("deductions") or []):
                name = (d.get("salary_component") or "").strip().lower()
                if name in {"bpjs jht employee", "bpjs jp employee"}:
                    jp_jht_employee_month += flt(d.get("amount", 0))

            # === 3) Hitung PPh21 Desember berbasis tahunan (December-only) ===
            result = calculate_pph21_december(
                employee=employee_doc,
                company=self.company,
                ytd_bruto_jan_nov=ytd_bruto_jan_nov,
                ytd_netto_jan_nov=ytd_netto_jan_nov,
                ytd_tax_paid_jan_nov=ytd_tax_paid_jan_nov,
                bruto_desember=bruto_desember,
                pengurang_netto_desember=pengurang_netto_desember,   # hanya untuk display
                biaya_jabatan_desember=biaya_jabatan_desember,
                # Dua opsi (pilih salah satu, yang bawah lebih eksplisit):
                # december_slip=slip_dict,
                jp_jht_employee_month=jp_jht_employee_month,
            )

            # Nilai pajak yang diposting untuk bulan Desember (koreksi)
            tax_amount = flt(result.get("pph21_bulan", 0.0))

            # Simpan ke field standar
            self.tax = tax_amount
            try:
                self.tax_type = "DECEMBER"
            except AttributeError:
                result["_tax_type"] = "DECEMBER"

            # Simpan detail ke pph21_info
            self.pph21_info = json.dumps(result)

            # Pastikan baris PPh21 di deductions ter-update
            self.update_pph21_row(tax_amount)

            # (Opsional) log audit
            frappe.logger().info(
                f"[DEC] {self.name} bruto_des={bruto_desember} bj_month={biaya_jabatan_desember} "
                f"jp_jht_month={jp_jht_employee_month} ytd_pph={ytd_tax_paid_jan_nov} -> tax_dec={tax_amount}"
            )
            return tax_amount

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.log_error(
                message=f"Failed to calculate December income tax: {e}\n{traceback.format_exc()}",
                title=f"Payroll Indonesia December Calculation Error - {self.name}",
            )
            raise frappe.ValidationError(f"Error in December PPh21 calculation: {e}")
        
    # -------------------------
    # Utilitas lain
    # -------------------------
    def _calculate_taxable_income(self):
        # Get base from Salary Structure Assignment
        base = 0
        bebas_kesehatan = 0
        bebas_jht = 0
        bebas_jp = 0
        try:
            ssa = frappe.db.get_value(
                "Salary Structure Assignment",
                {
                    "employee": self.employee,
                    "salary_structure": self.salary_structure,
                    "docstatus": 1
                },
                "base"
            )
            if ssa:
                base = flt(ssa)
        except Exception:
            base = 0

        try:
            emp_doc = self.get_employee_doc()
            if isinstance(emp_doc, dict):
                bebas_kesehatan = emp_doc.get("bebas_bpjs_kesehatan", 0)
                bebas_jht = emp_doc.get("bebas_bpjs_jht", 0)
                bebas_jp = emp_doc.get("bebas_bpjs_jp", 0)
            else:
                bebas_kesehatan = getattr(emp_doc, "bebas_bpjs_kesehatan", 0)
                bebas_jht = getattr(emp_doc, "bebas_bpjs_jht", 0)
                bebas_jp = getattr(emp_doc, "bebas_bpjs_jp", 0)
        except Exception:
            pass

        return {
            "earnings": [r.as_dict() for r in (self.earnings or [])],
            "deductions": [r.as_dict() for r in (self.deductions or [])],
            "employer_contributions": [r.as_dict() for r in (self.employer_contributions or [])],
            "base": base,
            "bebas_bpjs_kesehatan": bebas_kesehatan,
            "bebas_bpjs_jht": bebas_jht,
            "bebas_bpjs_jp": bebas_jp,
            "employee": getattr(self, "employee", None),
            "salary_structure": getattr(self, "salary_structure", None),
            "start_date": getattr(self, "start_date", None),
            "name": getattr(self, "name", None),
        }

    def update_pph21_row(self, tax_amount: float):
        try:
            target = "PPh 21"
            found = False
            for d in self.deductions:
                sc = d.get("salary_component") if isinstance(d, dict) else getattr(d, "salary_component", None)
                if sc == target:
                    if isinstance(d, dict):
                        d["amount"] = tax_amount
                    else:
                        d.amount = tax_amount
                    found = True
                    break
            if not found:
                self.append("deductions", {"salary_component": target, "amount": tax_amount})
            self._recalculate_totals()
        except Exception as e:
            frappe.log_error(
                message=f"Failed to update PPh21 row for {self.name}: {e}\n{traceback.format_exc()}",
                title="Payroll Indonesia PPh21 Row Update Error",
            )
            raise frappe.ValidationError(f"Error updating PPh21 component: {e}")

    def _recalculate_totals(self):
        try:
            if hasattr(self, "set_totals") and callable(getattr(self, "set_totals")):
                self.set_totals()
            elif hasattr(self, "calculate_totals") and callable(getattr(self, "calculate_totals")):
                self.calculate_totals()
            elif hasattr(self, "calculate_net_pay") and callable(getattr(self, "calculate_net_pay")):
                self.calculate_net_pay()
            else:
                self._manual_totals_calculation()
            self._update_rounded_values()
        except Exception:
            # fallback manual
            self._manual_totals_calculation()
            self._update_rounded_values()

    def _manual_totals_calculation(self):
        def row_amount(row):
            return row.get("amount", 0) if isinstance(row, dict) else getattr(row, "amount", 0)

        def flag(row, name):
            return (row.get(name, 0) if isinstance(row, dict) else getattr(row, name, 0)) or 0

        def include(row):
            return not (flag(row, "do_not_include_in_total") or flag(row, "statistical_component"))

        self.gross_pay = sum(row_amount(r) for r in (self.earnings or []) if include(r))
        self.total_deduction = sum(row_amount(r) for r in (self.deductions or []) if include(r))
        self.net_pay = (self.gross_pay or 0) - (self.total_deduction or 0)
        if hasattr(self, "total"):
            self.total = self.net_pay

    def _update_rounded_values(self):
        try:
            if hasattr(self, "rounded_total") and hasattr(self, "total"):
                self.rounded_total = round(getattr(self, "total", self.net_pay))
            if hasattr(self, "rounded_net_pay"):
                self.rounded_net_pay = round(self.net_pay)
            if hasattr(self, "net_pay_in_words"):
                try:
                    from frappe.utils import money_in_words
                    self.net_pay_in_words = money_in_words(self.net_pay, getattr(self, "currency", "IDR"))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to update rounded values for {self.name}: {e}")

    def populate_employer_contributions(self):
        """Salin komponen Employer dari earnings ke tabel employer_contributions."""
        try:
            from imogi_finance.payroll.employer_contributions import sync_doc_employer_contributions

            sync_doc_employer_contributions(self)
        except ImportError:
            self.set("employer_contributions", [])


    def _strip_bpjs_if_exempt(self):
        """Hapus komponen BPJS dari earnings/deductions berdasarkan field bebas_bpjs per-jenis."""
        try:
            emp_doc = self.get_employee_doc()
            if isinstance(emp_doc, dict):
                bebas_kesehatan = emp_doc.get("bebas_bpjs_kesehatan", 0)
                bebas_jht = emp_doc.get("bebas_bpjs_jht", 0)
                bebas_jp = emp_doc.get("bebas_bpjs_jp", 0)
            else:
                bebas_kesehatan = getattr(emp_doc, "bebas_bpjs_kesehatan", 0)
                bebas_jht = getattr(emp_doc, "bebas_bpjs_jht", 0)
                bebas_jp = getattr(emp_doc, "bebas_bpjs_jp", 0)
            if not any([bebas_kesehatan, bebas_jht, bebas_jp]):
                return
            def is_bpjs(component_name):
                n = (component_name or "").lower()
                if bebas_kesehatan and ("kesehatan" in n or ("bpjs" in n and "jht" not in n and "jp" not in n and "jkk" not in n and "jkm" not in n)):
                    return True
                if bebas_jht and ("jht" in n):
                    return True
                if bebas_jp and ("jp" in n and "bpjs" in n):
                    return True
                if bebas_kesehatan and ("jkk" in n or "jkm" in n):
                    return True
                return False

            for row in (self.earnings or []):
                sc = row.get("salary_component") if isinstance(row, dict) else getattr(row, "salary_component", "")
                if is_bpjs(sc):
                    if isinstance(row, dict): row["amount"] = 0
                    else: row.amount = 0

            for row in (self.deductions or []):
                sc = row.get("salary_component") if isinstance(row, dict) else getattr(row, "salary_component", "")
                if is_bpjs(sc):
                    if isinstance(row, dict): row["amount"] = 0
                    else: row.amount = 0

            for row in (self.get("employer_contributions") or []):
                sc = row.get("salary_component") if isinstance(row, dict) else getattr(row, "salary_component", "")
                if is_bpjs(sc):
                    if isinstance(row, dict): row["amount"] = 0
                    else: row.amount = 0

        except Exception as e:
            frappe.log_error(
                message=f"Failed to strip BPJS for {self.name}: {e}",
                title="Payroll Indonesia BPJS Exempt Error"
            )

    # -------------------------
    # Hook validate & sync history
    # -------------------------
    def validate(self):
        try:
            try:
                super().validate()
            except frappe.ValidationError:
                raise
            except Exception as e:
                frappe.log_error(
                    message=f"Error in parent validate for Salary Slip {self.name}: {e}\n{traceback.format_exc()}",
                    title="Payroll Indonesia Validation Error",
                )
            
            self.populate_employer_contributions()

            if getattr(self, "tax_type", "") == "DECEMBER":
                tax_amount = self.calculate_income_tax_december()
            else:
                tax_amount = self.calculate_income_tax()

            self.update_pph21_row(tax_amount)
            self._strip_bpjs_if_exempt()
            logger.info(f"Validate: Updated PPh21 deduction row to {tax_amount}")

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.log_error(
                message=f"Failed to update PPh21 in validate for Salary Slip {self.name}: {e}\n{traceback.format_exc()}",
                title="Payroll Indonesia PPh21 Update Error",
            )
            raise frappe.ValidationError(f"Error calculating PPh21: {e}")

    # -------------------------
    # Annual Payroll History sync
    # -------------------------
    def sync_to_annual_payroll_history(self, result, mode="monthly"):
        # Catatan: Bila Anda TIDAK ingin menulis APH sama sekali,
        # Anda bisa menonaktifkan pemanggilan fungsi ini di on_submit/on_cancel.
        if getattr(self, "_annual_history_synced", False):
            return

        try:
            if not getattr(self, "employee", None):
                logger.warning(f"No employee for Salary Slip {getattr(self, 'name', 'unknown')}, skip sync")
                return

            employee_doc = self.get_employee_doc() or {}
            employee_info = {
                "name": employee_doc.get("name") or self.employee,
                "company": employee_doc.get("company") or getattr(self, "company", None),
                "employee_name": employee_doc.get("employee_name"),
            }

            fiscal_year = get_aph_fiscal_year_from_salary_slip(self)
            if not fiscal_year:
                logger.warning(f"Could not determine fiscal year for Salary Slip {self.name}, skipping sync")
                return

            nomor_bulan = self._get_bulan_number(
                start_date=getattr(self, "start_date", None),
                nama_bulan=getattr(self, "bulan", None),
                end_date=getattr(self, "end_date", None),
            )

            raw_rate = result.get("rate", 0)
            numeric_rate = raw_rate if isinstance(raw_rate, (int, float)) else 0

            monthly_result = build_monthly_aph_row_from_salary_slip(self)
            monthly_result.update(
                {
                    "bulan": nomor_bulan,
                    "bruto": monthly_result.get("bruto") or result.get("bruto", result.get("bruto_total", 0)),
                    "pengurang_netto": monthly_result.get("pengurang_netto")
                    or result.get("pengurang_netto", result.get("income_tax_deduction_total", 0)),
                    "biaya_jabatan": monthly_result.get("biaya_jabatan")
                    or result.get("biaya_jabatan", result.get("biaya_jabatan_total", 0)),
                    "netto": monthly_result.get("netto") or result.get("netto", result.get("netto_total", 0)),
                    "pkp": monthly_result.get("pkp") or result.get("pkp", result.get("pkp_annual", 0)),
                    "rate": monthly_result.get("rate") or flt(numeric_rate),
                    "pph21": monthly_result.get("pph21") or result.get("pph21", result.get("pph21_bulan", 0)),
                    "salary_slip": self.name,
                }
            )

            if mode == "monthly":
                sync_annual_payroll_history(
                    employee=employee_info, fiscal_year=fiscal_year, monthly_results=[monthly_result], summary=None
                )
            elif mode == "december":
                summary = {
                    "bruto_total": result.get("bruto_total", 0),
                    "netto_total": result.get("netto_total", 0),
                    "ptkp_annual": result.get("ptkp_annual", 0),
                    "pkp_annual": result.get("pkp_annual", 0),
                    "pph21_annual": result.get("pph21_annual", 0),
                    "koreksi_pph21": result.get("koreksi_pph21", 0),
                }
                if isinstance(raw_rate, str) and raw_rate:
                    summary["rate_slab"] = raw_rate
                sync_annual_payroll_history(
                    employee=employee_info, fiscal_year=fiscal_year, monthly_results=[monthly_result], summary=summary
                )

            self._annual_history_synced = True

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.log_error(
                message=f"Failed to sync Annual Payroll History for {getattr(self, 'name', 'unknown')}: {e}\n{traceback.format_exc()}",
                title="Payroll Indonesia Annual History Sync Error",
            )
            logger.warning(f"Annual Payroll History sync failed for {self.name}: {e}")

    def on_submit(self):
        try:
            info = json.loads(getattr(self, "pph21_info", "{}") or "{}")
        except Exception:
            info = {}
        tax_type = getattr(self, "tax_type", None) or info.get("_tax_type")
        if not tax_type:
            bulan = self._get_bulan_number(
                start_date=getattr(self, "start_date", None),
                end_date=getattr(self, "end_date", None),
            )
            if bulan == 12:
                tax_type = "DECEMBER"
        mode = "december" if tax_type == "DECEMBER" else "monthly"
        self.sync_to_annual_payroll_history(info, mode=mode)
        if getattr(self, "_annual_history_synced", False):
            frappe.logger().info(f"[SYNC] Salary Slip {self.name} synced to Annual Payroll History")


    def _restore_aph_if_cancelled(self):
        """Auto-restore Annual Payroll History jika ikut ter-cancel."""
        try:
            fiscal_year = get_aph_fiscal_year_from_salary_slip(self)
            if not fiscal_year or not getattr(self, "employee", None):
                return
            aph_name = frappe.db.get_value(
                "Annual Payroll History",
                {"employee": self.employee, "fiscal_year": fiscal_year},
                "name",
            )
            if frappe.db.exists("Annual Payroll History", aph_name):
                docstatus = frappe.db.get_value("Annual Payroll History", aph_name, "docstatus")
                if docstatus == 2:  # Cancelled
                    frappe.db.set_value("Annual Payroll History", aph_name, "docstatus", 1)
                    frappe.db.commit()
                    frappe.logger("payroll_indonesia").info(
                        f"Auto-restored Annual Payroll History {aph_name} from Cancelled to Submitted"
                    )
        except Exception as e:
            frappe.log_error(
                message=f"Failed to restore APH for {getattr(self, 'name', 'unknown')}: {e}",
                title="Payroll Indonesia APH Restore Error"
            )

    def on_cancel(self):
        if getattr(self, "flags", {}).get("from_annual_payroll_cancel"):
            return
        # Prevent APH from being cancelled when salary slip is cancelled
        if not hasattr(self, 'ignore_linked_doctypes'):
            self.ignore_linked_doctypes = []
        if 'Annual Payroll History' not in self.ignore_linked_doctypes:
            self.ignore_linked_doctypes.append('Annual Payroll History')
        # Auto-restore APH if it got cancelled
        self._restore_aph_if_cancelled()
        try:
            if not getattr(self, "employee", None):
                logger.warning(f"No employee for cancelled Salary Slip {getattr(self, 'name', 'unknown')}, skip")
                return

            fiscal_year = get_aph_fiscal_year_from_salary_slip(self)
            if not fiscal_year:
                logger.warning(f"Could not determine fiscal year for cancelled Salary Slip {self.name}, skipping sync")
                return

            sync_annual_payroll_history(
                employee=self.employee,
                fiscal_year=fiscal_year,
                monthly_results=None,
                summary=None,
                cancelled_salary_slip=self.name,
            )
            frappe.logger().info(f"[SYNC] Salary Slip {self.name} removed from Annual Payroll History")
        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.log_error(
                message=f"Failed to remove from Annual Payroll History on cancel for {getattr(self, 'name', 'unknown')}: {e}\n{traceback.format_exc()}",
                title="Payroll Indonesia Annual History Cancel Error",
            )
            logger.warning(f"Failed to update Annual Payroll History when cancelling {self.name}: {e}")



def strip_bpjs_hook(doc, method=None):
    """Hook validate yang dipanggil TERAKHIR untuk zero-out BPJS jika exempt."""
    try:
        emp = frappe.get_doc("Employee", doc.employee)
        bebas_kesehatan = getattr(emp, "bebas_bpjs_kesehatan", 0)
        bebas_jht = getattr(emp, "bebas_bpjs_jht", 0)
        bebas_jp = getattr(emp, "bebas_bpjs_jp", 0)
        if not any([bebas_kesehatan, bebas_jht, bebas_jp]):
            return
        def is_bpjs(name):
            n = (name or "").lower()
            if bebas_kesehatan and ("kesehatan" in n or "jkk" in n or "jkm" in n):
                return True
            if bebas_jht and "jht" in n:
                return True
            if bebas_jp and "jp" in n and "bpjs" in n:
                return True
            return False

        changed = False
        for d in (doc.deductions or []):
            sc = d.get("salary_component") if isinstance(d, dict) else getattr(d, "salary_component", "")
            if is_bpjs(sc):
                if isinstance(d, dict): d["amount"] = 0
                else: d.amount = 0
                changed = True

        for e in (doc.earnings or []):
            sc = e.get("salary_component") if isinstance(e, dict) else getattr(e, "salary_component", "")
            if is_bpjs(sc):
                if isinstance(e, dict): e["amount"] = 0
                else: e.amount = 0
                changed = True

        for row in (doc.get("employer_contributions") or []):
            sc = row.get("salary_component") if isinstance(row, dict) else getattr(row, "salary_component", "")
            if is_bpjs(sc):
                if isinstance(row, dict): row["amount"] = 0
                else: row.amount = 0
                changed = True

        if changed:
            # Recalculate totals setelah zero-out, mirroring HRMS's own
            # calculate_net_pay()/set_net_pay() field-by-field so every
            # derived total (base_* and rounded_total) stays consistent -
            # not just net_pay/total_deduction.
            doc.gross_pay = sum(
                (r.get("amount", 0) if isinstance(r, dict) else getattr(r, "amount", 0))
                for r in (doc.earnings or [])
                if not (r.get("do_not_include_in_total") if isinstance(r, dict) else getattr(r, "do_not_include_in_total", 0))
            )
            doc.base_gross_pay = flt(
                flt(doc.gross_pay) * flt(doc.exchange_rate), doc.precision("base_gross_pay")
            )
            doc.total_deduction = sum(
                (r.get("amount", 0) if isinstance(r, dict) else getattr(r, "amount", 0))
                for r in (doc.deductions or [])
                if not (r.get("do_not_include_in_total") if isinstance(r, dict) else getattr(r, "do_not_include_in_total", 0))
            )
            doc.base_total_deduction = flt(
                flt(doc.total_deduction) * flt(doc.exchange_rate), doc.precision("base_total_deduction")
            )
            doc.net_pay = flt(doc.gross_pay) - (
                flt(doc.total_deduction) + flt(doc.get("total_loan_repayment"))
            )
            doc.rounded_total = rounded(doc.net_pay)
            doc.base_net_pay = flt(
                flt(doc.net_pay) * flt(doc.exchange_rate), doc.precision("base_net_pay")
            )
            doc.base_rounded_total = flt(
                rounded(doc.base_net_pay), doc.precision("base_net_pay")
            )

    except Exception as e:
        frappe.log_error(
            message=f"Failed to strip BPJS in hook for {getattr(doc, 'name', 'unknown')}: {e}",
            title="Payroll Indonesia BPJS Strip Hook Error"
        )


def on_submit(doc, method=None):
    if isinstance(doc, CustomSalarySlip):
        return
    doc.__class__ = CustomSalarySlip
    doc.on_submit()


def on_cancel(doc, method=None):
    if isinstance(doc, CustomSalarySlip):
        return
    doc.__class__ = CustomSalarySlip
    doc.on_cancel()
