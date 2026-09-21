# Distribution Pipeline

Every canonical signal independently creates an audit route, a shadow
execution route per portfolio/account, and an `INTERNAL_QUEUE`
`DistributionIntent`. Distribution is not conditional on sizing acceptance;
an account rejection cannot suppress the signal queue. External webhooks,
email, SMS, WhatsApp, and app push are future adapters only.
