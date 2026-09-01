# Copyright 2019 Komit <https://komit-consulting.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from email.utils import getaddresses

from email_validator import EmailSyntaxError, EmailUndeliverableError, validate_email

from odoo import api, models
from odoo.exceptions import UserError, ValidationError


class ResPartner(models.Model):
    _inherit = "res.partner"

    def copy_data(self, default=None):
        res = super().copy_data(default=default)
        if self._should_filter_duplicates():
            for copy_vals in res:
                copy_vals.pop("email", None)
        return res

    @api.model
    def email_check(self, emails, company=None):
        """Normalize a field value under ``company``'s validation settings.

        ``company`` is the company of the record being written, which is not
        always ``self.env.company``: the mail gateway runs as a service user
        whose default company has nothing to do with the contact it is
        creating. Judging one company's data by another's settings is what
        took inbound mail down twice.
        """
        if not self._should_check_syntax(company=company):
            # Checking is off for this company, so store what was given.
            # Splitting and rejoining would still rewrite the value, which is
            # not what "don't check my addresses" asks for.
            return emails
        return ",".join(
            self._normalize_email(email, company=company)
            for email in self._email_check_split(emails)
        )

    def _pec_company(self, company=None):
        """The company whose validation settings govern this operation."""
        if company:
            return company
        if self and "company_id" in self._fields:
            companies = self.mapped("company_id")
            # A mixed batch has no single answer; fall back rather than pick.
            if len(companies) == 1:
                return companies
        return self.env.company

    @api.model
    def _email_check_split(self, emails):
        """Split a possibly multi-address field value into single addresses.

        RFC 5322 lets a display name contain a comma, and Outlook's default
        "Last, First" format uses one, so a plain ``emails.split(",")`` tears
        ``"Silva, Klaire" <k@example.com>`` into the two invalid fragments
        ``"Silva`` and ``Klaire" <k@example.com>``. That matters beyond typing:
        the mail gateway feeds raw ``From`` headers into this field, so a
        contact whose name holds a comma made inbound mail unprocessable.

        ``getaddresses`` parses quoting properly, but it answers a malformed
        value with a single empty address and drops the rest of the input with
        it -- so when it yields nothing usable, fall back to the naive split.
        That keeps a bad address reported as itself instead of as the whole
        field value.
        """
        if not emails or not emails.strip():
            return []
        parsed = [
            address.strip()
            for _name, address in getaddresses([emails])
            if address.strip()
        ]
        if parsed:
            return parsed
        return [fragment.strip() for fragment in emails.split(",") if fragment.strip()]

    @api.constrains("email")
    def _check_email_unique(self):
        # Constraint methods run as superuser (see BaseModel._validate_fields),
        # so this search is authoritative across all records and companies,
        # regardless of the acting user's record rules. The acting user's own
        # access rights are only used to decide how much detail to disclose in
        # the error message.
        acting_user_model = self.sudo(False)
        if not self._should_filter_duplicates():
            return
        global_scope = self._should_filter_duplicates_globally()
        for rec in self.filtered("email"):
            if "," in rec.email:
                raise UserError(
                    self.env._(
                        "Field contains multiple email addresses. This is "
                        "not supported when duplicate email addresses are "
                        "not allowed."
                    )
                )
            domain = [("email", "=", rec.email), ("id", "!=", rec.id)]
            if not global_scope:
                # Per-company scope: only records sharing the same company
                # (including company-agnostic records, company_id = False) are
                # considered duplicates.
                domain.append(("company_id", "=", rec.company_id.id))
            conflict = self.search(domain, limit=1)
            if not conflict:
                continue
            # Disclose the conflicting record only if the acting user is
            # actually allowed to see it; otherwise keep its identity private.
            if acting_user_model.search_count([("id", "=", conflict.id)], limit=1):
                raise UserError(
                    self.env._(
                        "Email address %(email)s is already in use by "
                        "%(partner)s (ID: %(partner_id)s). Please input "
                        "another email address or use the existing record.",
                        email=rec.email.strip(),
                        partner=conflict.display_name,
                        partner_id=conflict.id,
                    )
                )
            raise UserError(
                self.env._(
                    "Email address %(email)s is already in use by a record "
                    "you do not have access to. Please input a different "
                    "email address, or contact your system administrator to "
                    "request access.",
                    email=rec.email.strip(),
                )
            )

    def _normalize_email(self, email, company=None):
        if not self._should_check_syntax(company=company):
            return email
        try:
            result = validate_email(
                email,
                check_deliverability=self._should_check_deliverability(company=company),
            )
        except EmailSyntaxError:
            raise ValidationError(
                self.env._("%s is an invalid email", email.strip())
            ) from EmailSyntaxError
        except EmailUndeliverableError:
            raise ValidationError(
                self.env._("Cannot deliver to email address %s", email.strip())
            ) from EmailUndeliverableError
        return result.normalized.lower()

    def _should_check_syntax(self, company=None):
        """Whether to validate the address, and so also normalize it.

        ``partner_email_check_skip_syntax`` in the context turns this off for a
        single operation, for the same reason as
        ``_should_check_deliverability``. It skips normalization too, exactly as
        disabling the company setting does.
        """
        if self.env.context.get("partner_email_check_skip_syntax"):
            return False
        return self._pec_company(company).partner_email_check_syntax

    def _should_filter_duplicates(self, company=None):
        """Whether duplicate addresses are rejected.

        Unlike syntax and deliverability, this deliberately reads
        ``env.company`` rather than the record's company. "Across all
        companies" promises the address is used once in the whole database, so
        a company that has switched duplicate filtering off must not become a
        place where another company's claimed address can be parked. The
        ``company`` argument is accepted for signature symmetry with the other
        checks and intentionally unused.

        ``partner_email_check_skip_duplicates`` in the context turns this off
        for a single operation, for the same reason as the other two skips:
        the mail gateway records addresses it did not collect from a user, and
        a sender who happens to collide with an existing contact must not be
        able to stop mail from being delivered. That is the right lever for
        the gateway -- not weakening what the setting means.
        """
        if self.env.context.get("partner_email_check_skip_duplicates"):
            return False
        return self.env.company.partner_email_check_filter_duplicates

    def _should_filter_duplicates_globally(self, company=None):
        return self.env.company.partner_email_check_duplicate_scope == "global"

    def _should_check_deliverability(self, company=None):
        """Whether to require that the address' domain accepts mail.

        ``partner_email_check_skip_deliverability`` in the context turns this
        off for a single operation. Code that stores addresses it did not
        collect from a user needs that: the incoming mail gateway creates a
        partner for the sender of every message, and bulk senders routinely send
        from a subdomain that publishes no MX record, so a hard check there
        discards the message rather than improving anyone's data.
        """
        if self.env.context.get("partner_email_check_skip_deliverability"):
            return False
        return self._pec_company(company).partner_email_check_check_deliverability

    @api.model_create_multi
    def create(self, vals_list):
        Company = self.env["res.company"]
        for vals in vals_list:
            if vals.get("email"):
                # The record does not exist yet, so its company can only come
                # from the values being written.
                vals["email"] = self.email_check(
                    vals["email"], company=Company.browse(vals.get("company_id"))
                )
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("email"):
            company = (
                self.env["res.company"].browse(vals["company_id"])
                if vals.get("company_id")
                else self._pec_company()
            )
            vals["email"] = self.email_check(vals["email"], company=company)
        return super().write(vals)
