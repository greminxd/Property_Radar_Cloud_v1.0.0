import secrets
print('SESSION_SECRET='+secrets.token_urlsafe(48))
print('TELEGRAM_WEBHOOK_SECRET='+secrets.token_urlsafe(32).replace('-','A').replace('_','B')[:48])
