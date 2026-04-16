import frappe

no_cache = 1


def get_context(context):
	context.csrf_token = frappe.sessions.get_csrf_token()
