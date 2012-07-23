import datetime
from random import random

from django.conf import settings
from django.db import models, IntegrityError
from django.core.mail import send_mail
from django.core.urlresolvers import reverse, NoReverseMatch
from django.template.loader import render_to_string
from django.utils.hashcompat import sha_constructor
from django.utils.translation import gettext_lazy as _

from django.contrib.sites.models import Site
from django.contrib.auth.models import User

from emailconfirmation.signals import email_confirmed, email_confirmation_sent

try:
    from django.utils.timezone import now
except ImportError:
    now = datetime.datetime.now

# this code based in-part on django-registration

class EmailAddressManager(models.Manager):
    
    def add_email(self, user, email):
        try:
            email_address = self.create(user=user, email=email)
            EmailConfirmation.objects.send_confirmation(email_address)
            return email_address
        except IntegrityError:
            return None
    
    def get_primary(self, user):
        try:
            return self.get(user=user, primary=True)
        except EmailAddress.DoesNotExist:
            return None
    
    def get_users_for(self, email):
        """
        returns a list of users with the given email.
        """
        # this is a list rather than a generator because we probably want to
        # do a len() on it right away
        return [address.user for address in EmailAddress.objects.filter(
            verified=True, email=email)]


class EmailAddress(models.Model):
    
    user = models.ForeignKey(User)
    email = models.EmailField()
    verified = models.BooleanField(default=False)
    primary = models.BooleanField(default=False)
    
    objects = EmailAddressManager()
    
    def set_as_primary(self):
        old_primary = EmailAddress.objects.get_primary(self.user)
        if old_primary:
            old_primary.primary = False
            old_primary.save()
        self.primary = True
        self.save()
        self.user.email = self.email
        if getattr(settings, 'EMAIL_CONFIRMATION_OVERWRITE_USERNAME', False):
            self.user.username = self.email
        self.user.save()
        return True
    
    def __unicode__(self):
        return u"%s (%s)" % (self.email, self.user)
    
    class Meta:
        verbose_name = _("email address")
        verbose_name_plural = _("email addresses")
        unique_together = (
            ("user", "email"),
        )


class EmailConfirmationManager(models.Manager):

    def generate_key(self, email):
        """
        Generate a new email confirmation key and return it.

        The key is a hash of:
           * time specific data
           * the email address it's being generated for
           * a random salt.
        """
        payload = ''.join([
            str(now()),
            str(email),
            sha_constructor(str(random())).hexdigest(),
        ])
        return sha_constructor(payload).hexdigest()

    def create_emailconfirmation(self, email_address):
        "Create an email confirmation obj from the given email address obj"
        confirmation_key = self.generate_key(email_address.email)
        confirmation = self.create(
            email_address=email_address,
            created_at=now(),
            key=confirmation_key,
        )
        return confirmation
    
    def confirm_email(self, key, make_primary=True):
        try:
            confirmation = self.get(key=key)
        except self.model.DoesNotExist:
            return None
        if not confirmation.key_expired():
            email_address = confirmation.email_address
            email_address.verified = True
            if make_primary:
                email_address.set_as_primary()
            email_address.save()
            email_confirmed.send(sender=self.model, email_address=email_address)
            return email_address
    
    def send_confirmation(self, email_address):
        confirmation = self.create_email_confirmation(email_address)
        current_site = Site.objects.get_current()
        # check for the url with the dotted view path
        try:
            path = reverse("emailconfirmation.views.confirm_email",
                args=[confirmation.key])
        except NoReverseMatch:
            # or get path with named urlconf instead
            path = reverse(
                "emailconfirmation_confirm_email", args=[confirmation.key])
        protocol = getattr(settings, "DEFAULT_HTTP_PROTOCOL", "http")
        activate_url = u"%s://%s%s" % (
            protocol,
            unicode(current_site.domain),
            path
        )
        context = {
            "user": email_address.user,
            "activate_url": activate_url,
            "current_site": current_site,
            "confirmation_key": confirmation.key,
        }
        subject = render_to_string(
            "emailconfirmation/email_confirmation_subject.txt", context)
        # remove superfluous line breaks
        subject = "".join(subject.splitlines())
        message = render_to_string(
            "emailconfirmation/email_confirmation_message.txt", context)
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email_address.email])
        email_confirmation_sent.send(
            sender=self.model,
            confirmation=confirmation,
        )
        return confirmation
    
    def delete_expired_confirmations(self):
        for confirmation in self.all():
            if confirmation.key_expired():
                confirmation.delete()


class EmailConfirmation(models.Model):
    
    email_address = models.ForeignKey(EmailAddress)
    created_at = models.DateTimeField()
    key = models.CharField(max_length=40)
    
    objects = EmailConfirmationManager()
    
    def key_expired(self):
        confirmation_days = getattr(settings, 'EMAIL_CONFIRMATION_DAYS', 7)
        expiration_date = self.created_at + datetime.timedelta(
                days=confirmation_days)
        return expiration_date <= now()
    key_expired.boolean = True
    
    def __unicode__(self):
        return u"confirmation for %s" % self.email_address
    
    class Meta:
        verbose_name = _("email confirmation")
        verbose_name_plural = _("email confirmations")
