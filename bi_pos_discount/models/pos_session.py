# -*- coding: utf-8 -*-
# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import models


class POSOrderLoad(models.Model):
	_inherit = 'pos.session'

	def _pos_ui_models_to_load(self):
		result = super()._pos_ui_models_to_load()
		new_model = 'pos.order'
		if new_model not in result:
			result.append(new_model)
		return result

	def _loader_params_pos_order(self):
		return {
			'search_params': {
				'fields': [
					'discount_type',
				],
			}
		}

	def _get_pos_ui_pos_order(self, params):
		return self.env['pos.order'].search_read(**params['search_params'])

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4: