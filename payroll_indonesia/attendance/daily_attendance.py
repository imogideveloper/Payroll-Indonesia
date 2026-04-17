import frappe
from frappe.utils import today, add_days

def process_yesterday_attendance():
    """Mark absent untuk yang tidak checkin kemarin"""
    
    yesterday = add_days(today(), -1)
    
    employees = frappe.get_all("Employee",
        filters={"status": "Active"},
        fields=["name", "employee_name", "company"]
    )
    
    for emp in employees:
        existing = frappe.db.exists("Attendance", {
            "employee": emp.name,
            "attendance_date": yesterday
        })
        
        if existing:
            continue
        
        has_checkin = frappe.db.exists("Employee Checkin", {
            "employee": emp.name,
            "time": ["between", [f"{yesterday} 00:00:00", f"{yesterday} 23:59:59"]]
        })
        
        if not has_checkin:
            create_absent_attendance(emp, yesterday)

def create_absent_attendance(employee_data, date):
    """Create Absent attendance"""
    
    try:
        attendance = frappe.get_doc({
            "doctype": "Attendance",
            "employee": employee_data.name,
            "employee_name": employee_data.employee_name,
            "attendance_date": date,
            "status": "Absent",
            "company": employee_data.company,
            "working_hours": 0,
            "naming_series": "HR-ATT-.YYYY.-"
        })
        
        attendance.insert(ignore_permissions=True)
        attendance.submit()
        frappe.db.commit()
        
    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title=f"Absent Attendance Failed")
