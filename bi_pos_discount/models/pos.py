# -*- coding: utf-8 -*-
# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.
import logging
from datetime import timedelta
from functools import partial

import psycopg2
import pytz
import re
from odoo import api, fields, models, tools, _
from odoo.tools import float_is_zero
from odoo.exceptions import UserError
from odoo.http import request
import odoo.addons.decimal_precision as dp
from odoo.osv.expression import AND

_logger = logging.getLogger(__name__)



class PosConfiguration(models.Model):
	_inherit = 'pos.config'

	discount_type = fields.Selection([('percentage', "Percentage"), ('fixed', "Fixed")], string='Discount Type',
									 default='percentage', help='Seller can apply different Discount Type in POS.')


class ResConfigSettings(models.TransientModel):
	_inherit = 'res.config.settings'

	discount_type = fields.Selection(related='pos_config_id.discount_type',readonly=False,required=True)


class PosOrderLine(models.Model):
	_inherit = 'pos.order.line'

	def _export_for_ui(self, orderline):
		return {
			'qty': orderline.qty, 'price_unit': orderline.price_unit, 'price_subtotal': orderline.price_subtotal,
			'price_subtotal_incl': orderline.price_subtotal_incl, 'product_id': orderline.product_id.id,
			'discount': orderline.discount, 'tax_ids': [[6, False, orderline.tax_ids.mapped(lambda tax: tax.id)]],
			'id': orderline.id, 'pack_lot_ids': [[0, 0, lot] for lot in orderline.pack_lot_ids.export_for_ui()],
			'customer_note': orderline.customer_note, 'refunded_qty': orderline.refunded_qty,
			'discount_type': orderline.order_id.discount_type}


class PosOrder(models.Model):
	_inherit = 'pos.order'

	discount_type = fields.Char(string='Discount Type')

	def _export_for_ui(self, order):
		timezone = pytz.timezone(self._context.get('tz') or self.env.user.tz or 'UTC')
		return {
			'lines': [[0, 0, line] for line in order.lines.export_for_ui()],
			'statement_ids': [[0, 0, payment] for payment in order.payment_ids.export_for_ui()],
			'name': order.pos_reference,
			'uid': re.search('([0-9]|-){14}', order.pos_reference).group(0),
			'amount_paid': order.amount_paid,
			'amount_total': order.amount_total,
			'amount_tax': order.amount_tax,
			'amount_return': order.amount_return,
			'pos_session_id': order.session_id.id,
			'is_session_closed': order.session_id.state == 'closed',
			'pricelist_id': order.pricelist_id.id,
			'partner_id': order.partner_id.id,
			'user_id': order.user_id.id,
			'sequence_number': order.sequence_number,
			'creation_date': order.date_order.astimezone(timezone),
			'fiscal_position_id': order.fiscal_position_id.id,
			'to_invoice': order.to_invoice,
			'to_ship': order.to_ship,
			'state': order.state,
			'discount_type': order.discount_type,
			'account_move': order.account_move.id,
			'id': order.id,
			'is_tipped': order.is_tipped,
			'tip_amount': order.tip_amount,
		}

	def _prepare_invoice_vals(self):
		res = super(PosOrder, self)._prepare_invoice_vals()
		res.update({'pos_order_id': self.id})
		return res

	def _prepare_invoice_line(self, order_line):
		res = super(PosOrder, self)._prepare_invoice_line(order_line)
		res.update({'pos_order_line_id': order_line.id, 'pos_order_id': self.id})
		return res

	@api.model
	def _amount_line_tax(self, line, fiscal_position_id):
		taxes = line.tax_ids.filtered(lambda t: t.company_id.id == line.order_id.company_id.id)
		taxes = fiscal_position_id.map_tax(taxes)
		if line.discount_line_type == 'Percentage':
			price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
		else:
			price = line.price_unit - line.discount
		taxes = taxes.compute_all(price, line.order_id.pricelist_id.currency_id, line.qty, product=line.product_id, partner=line.order_id.partner_id or False)['taxes']
		return sum(tax.get('amount', 0.0) for tax in taxes)

	@api.onchange('payment_ids', 'lines')
	def _onchange_amount_all(self):
		for order in self:
			currency = order.pricelist_id.currency_id
			order.amount_paid = sum(payment.amount for payment in order.payment_ids)
			order.amount_return = sum(payment.amount < 0 and payment.amount or 0 for payment in order.payment_ids)
			order.amount_tax = currency.round(
				sum(self._amount_line_tax(line, order.fiscal_position_id) for line in order.lines))
			amount_untaxed = currency.round(sum(line.price_subtotal for line in order.lines))
			order.amount_total = order.amount_tax + amount_untaxed

	@api.model
	def _process_order(self, order, draft, existing_order):
		"""Create or update an pos.order from a given dictionary.

		:param dict order: dictionary representing the order.
		:param bool draft: Indicate that the pos_order is not validated yet.
		:param existing_order: order to be updated or False.
		:type existing_order: pos.order.
		:returns: id of created/updated pos.order
		:rtype: int
		"""
		order = order['data']
		pos_session = self.env['pos.session'].browse(order['pos_session_id'])
		if pos_session.state == 'closing_control' or pos_session.state == 'closed':
			order['pos_session_id'] = self._get_valid_session(order).id
		pos_order = False
		if not existing_order:
			pos_order = self.create(self._order_fields(order))
		else:
			pos_order = existing_order
			pos_order.lines.unlink()
			order['user_id'] = pos_order.user_id.id
			pos_order.write(self._order_fields(order))
		if order['discount_type']:
			if order['discount_type'] == 'Percentage':
				pos_order.update({'discount_type': "Percentage"})
				pos_order.lines.update({'discount_line_type': "Percentage"})
			if order['discount_type'] == 'Fixed':
				pos_order.update({'discount_type': "Fixed"})
				pos_order.lines.update({'discount_line_type': "Fixed"})
		else:
			if pos_order.config_id.discount_type == 'percentage':
				pos_order.update({'discount_type': "Percentage"})
				pos_order.lines.update({'discount_line_type': "Percentage"})
			if pos_order.config_id.discount_type == 'fixed':
				pos_order.update({'discount_type': "Fixed"})
				pos_order.lines.update({'discount_line_type': "Fixed"})	

		pos_order = pos_order.with_company(pos_order.company_id)
		self = self.with_company(pos_order.company_id)
		self._process_payment_lines(order, pos_order, pos_session, draft)

		if not draft:
			try:
				pos_order.action_pos_order_paid()
			except psycopg2.DatabaseError:
				# do not hide transactional errors, the order(s) won't be saved!
				raise
			except Exception as e:
				_logger.error('Could not fully process the POS Order: %s', tools.ustr(e))
			pos_order._create_order_picking()
			pos_order._compute_total_cost_in_real_time()
		if pos_order.to_invoice and pos_order.state == 'paid':
			pos_order._generate_pos_order_invoice()
			if pos_order.discount_type and pos_order.discount_type == "Fixed":
				invoice = pos_order.account_move
				for line in invoice.invoice_line_ids:
					pos_line = line.pos_order_line_id
					if pos_line and pos_line.discount_line_type == "Fixed":
						line.write({'price_unit': pos_line.price_unit})
		return pos_order.id


class PosOrderLine(models.Model):
	_inherit = 'pos.order.line'

	discount_line_type = fields.Char(string='Discount Type', readonly=True)

	def _compute_amount_line_all(self):
		for line in self:
			fpos = line.order_id.fiscal_position_id
			tax_ids_after_fiscal_position = fpos.map_tax(line.tax_ids, line.product_id,
														 line.order_id.partner_id) if fpos else line.tax_ids
			if line.discount_line_type == "Fixed":
				price = line.price_unit - line.discount
			else:
				price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
			taxes = tax_ids_after_fiscal_position.compute_all(price, line.order_id.pricelist_id.currency_id, line.qty,
															  product=line.product_id, partner=line.order_id.partner_id)
			line.update({'price_subtotal_incl': taxes['total_included'], 'price_subtotal': taxes['total_excluded']})


class ReportSaleDetailsInherit(models.AbstractModel):
	_inherit = 'report.point_of_sale.report_saledetails'

	@api.model
	def get_sale_details(self, date_start=False, date_stop=False, config_ids=False, session_ids=False):
		""" Serialise the orders of the requested time period, configs and sessions.

		:param date_start: The dateTime to start, default today 00:00:00.
		:type date_start: str.
		:param date_stop: The dateTime to stop, default date_start + 23:59:59.
		:type date_stop: str.
		:param config_ids: Pos Config id's to include.
		:type config_ids: list of numbers.
		:param session_ids: Pos Config id's to include.
		:type session_ids: list of numbers.

		:returns: dict -- Serialised sales.
		"""
		domain = [('state', 'in', ['paid', 'invoiced', 'done'])]
		if (session_ids):
			domain = AND([domain, [('session_id', 'in', session_ids)]])
		else:
			if date_start:
				date_start = fields.Datetime.from_string(date_start)
			else:
				# start by default today 00:00:00
				user_tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
				today = user_tz.localize(fields.Datetime.from_string(fields.Date.context_today(self)))
				date_start = today.astimezone(pytz.timezone('UTC'))
			if date_stop:
				date_stop = fields.Datetime.from_string(date_stop)
				# avoid a date_stop smaller than date_start
				if (date_stop < date_start):
					date_stop = date_start + timedelta(days=1, seconds=-1)
			else:
				# stop by default today 23:59:59
				date_stop = date_start + timedelta(days=1, seconds=-1)
			domain = AND([domain,
				[('date_order', '>=', fields.Datetime.to_string(date_start)),
				('date_order', '<=', fields.Datetime.to_string(date_stop))]
			])
			if config_ids:
				domain = AND([domain, [('config_id', 'in', config_ids)]])
		orders = self.env['pos.order'].search(domain)
		user_currency = self.env.company.currency_id
		total = 0.0
		products_sold = {}
		taxes = {}
		for order in orders:
			if user_currency != order.pricelist_id.currency_id:
				total += order.pricelist_id.currency_id._convert(
					order.amount_total, user_currency, order.company_id, order.date_order or fields.Date.today())
			else:
				total += order.amount_total
			currency = order.session_id.currency_id
			for line in order.lines:
				key = (line.product_id, line.price_unit, line.discount, line.discount_line_type)
				products_sold.setdefault(key, 0.0)
				products_sold[key] += line.qty

				if line.tax_ids_after_fiscal_position:
					line_taxes = line.tax_ids_after_fiscal_position.sudo().compute_all(line.price_unit * (1-(line.discount or 0.0)/100.0), currency, line.qty, product=line.product_id, partner=line.order_id.partner_id or False)
					for tax in line_taxes['taxes']:
						taxes.setdefault(tax['id'], {'name': tax['name'], 'tax_amount':0.0, 'base_amount':0.0})
						taxes[tax['id']]['tax_amount'] += tax['amount']
						taxes[tax['id']]['base_amount'] += tax['base']
				else:
					taxes.setdefault(0, {'name': _('No Taxes'), 'tax_amount':0.0, 'base_amount':0.0})
					taxes[0]['base_amount'] += line.price_subtotal_incl

		payment_ids = self.env["pos.payment"].search([('pos_order_id', 'in', orders.ids)]).ids
		if payment_ids:
			self.env.cr.execute("""
				SELECT COALESCE(method.name->>%s, method.name->>'en_US') as name, sum(amount) total
				FROM pos_payment AS payment,
					 pos_payment_method AS method
				WHERE payment.payment_method_id = method.id
					AND payment.id IN %s
				GROUP BY method.name
			""", (self.env.lang, tuple(payment_ids),))
			payments = self.env.cr.dictfetchall()
		else:
			payments = []
		return {
			'date_start': date_start, 'date_stop': date_stop, 'currency_precision': user_currency.decimal_places,
			'total_paid': user_currency.round(total), 'payments': payments, 'company_name': self.env.company.name,
			'taxes': list(taxes.values()),
			'products': sorted([{
				'product_id': product.id,
				'product_name': product.name,
				'code': product.default_code,
				'quantity': qty,
				'discount_line_type': discount_line_type,
				'price_unit': price_unit,
				'discount': discount,
				'uom': product.uom_id.name
			} for (product, price_unit, discount, discount_line_type), qty in products_sold.items()], key=lambda l: l['product_name'])
		}

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4: