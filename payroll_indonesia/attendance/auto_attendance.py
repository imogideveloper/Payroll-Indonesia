import frappe
from frappe.utils import get_datetime, getdate
from datetime import timedelta

def auto_create_from_checkin(doc, method):
    """
    Auto-create Attendance setelah Employee Checkin
    """
    
    checkin_date = getdate(doc.time)
    employee = frappe.get_cached_doc("Employee", doc.employee)
    
    # Cek apakah Attendance sudah ada
    existing = frappe.db.get_value("Attendance", {
        "employee": doc.employee,
        "attendance_date": checkin_date,
        "docstatus": ["<", 2]
    }, "name")
    
    if existing:
        update_attendance_hours(existing, doc.employee, checkin_date)
        return
    
    # Ambil semua checkin hari ini
    checkins = frappe.get_all("Employee Checkin",
        filters={
            "employee": doc.employee,
            "time": ["between", [
                f"{checkin_date} 00:00:00",
                f"{checkin_date} 23:59:59"
            ]]
        },
        fields=["name", "log_type", "time"],
        order_by="time asc"
    )
    
    # Check IN dan OUT
    has_in = any(c.log_type == "IN" for c in checkins)
    has_out = any(c.log_type == "OUT" for c in checkins)
    
    if not (has_in and has_out):
        return
    
    # Calculate working hours
    first_in = next((c for c in checkins if c.log_type == "IN"), None)
    last_out = next((c for c in reversed(checkins) if c.log_type == "OUT"), None)
    
    working_hours = 0
    if first_in and last_out:
        time_diff = get_datetime(last_out.time) - get_datetime(first_in.time)
        working_hours = round(time_diff.total_seconds() / 3600, 2)
    
    status = determine_status(working_hours)
    
    try:
        attendance = frappe.get_doc({
            "doctype": "Attendance",
            "employee": doc.employee,
            "employee_name": employee.employee_name,
            "attendance_date": checkin_date,
            "status": status,
            "company": employee.company,
            "working_hours": working_hours,
            "naming_series": "HR-ATT-.YYYY.-"
        })
        
        attendance.insert(ignore_permissions=True)
        attendance.submit()
        frappe.db.commit()
        
    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title=f"Attendance Creation Failed")

def update_attendance_hours(attendance_name, employee, date):
    """Update working hours"""
    
    checkins = frappe.get_all("Employee Checkin",
        filters={
            "employee": employee,
            "time": ["between", [f"{date} 00:00:00", f"{date} 23:59:59"]]
        },
        fields=["log_type", "time"],
        order_by="time asc"
    )
    
    first_in = next((c for c in checkins if c.log_type == "IN"), None)
    last_out = next((c for c in reversed(checkins) if c.log_type == "OUT"), None)
    
    if first_in and last_out:
        time_diff = get_datetime(last_out.time) - get_datetime(first_in.time)
        working_hours = round(time_diff.total_seconds() / 3600, 2)
        
        attendance = frappe.get_doc("Attendance", attendance_name)
        if attendance.docstatus == 0:
            attendance.working_hours = working_hours
            attendance.status = determine_status(working_hours)
            attendance.save(ignore_permissions=True)
            frappe.db.commit()

def determine_status(working_hours):
    """Tentukan status"""
    if working_hours >= 8:
        return "Present"
    elif working_hours >= 4:
        return "Half Day"
    else:
        return "Absent"
