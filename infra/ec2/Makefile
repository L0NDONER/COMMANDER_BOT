# Host-level config sync for the EC2 box (nginx, fail2ban).
# App code goes via GitHub Actions; this is the parallel path for everything
# outside the docker-mounted repo. Files under ./etc/ mirror the remote /etc/.

HOST  := aws
LOCAL := etc/
RSYNC := rsync -av --rsync-path="sudo rsync" --no-owner --no-group

.PHONY: pull plan sync reload diff

# Pull current EC2 state down — catch up after manual edits or certbot renewals.
pull:
	rsync -av $(HOST):/etc/fail2ban/jail.local              etc/fail2ban/jail.local
	rsync -av $(HOST):/etc/fail2ban/jail.d/nginx-404.conf   etc/fail2ban/jail.d/nginx-404.conf
	rsync -av $(HOST):/etc/fail2ban/filter.d/nginx-404.conf etc/fail2ban/filter.d/nginx-404.conf
	rsync -av $(HOST):/etc/nginx/snippets/blocklist.conf    etc/nginx/snippets/blocklist.conf
	rsync -av $(HOST):/etc/nginx/sites-enabled/flaz-routing etc/nginx/sites-enabled/flaz-routing

# Dry-run: show what sync would change without writing anything.
plan:
	$(RSYNC) --dry-run --itemize-changes $(LOCAL) $(HOST):/etc/

# Push local → EC2, validate nginx, reload services.
sync:
	$(RSYNC) $(LOCAL) $(HOST):/etc/
	ssh $(HOST) "sudo nginx -t && sudo systemctl reload nginx && sudo fail2ban-client reload"
	@echo "OK infra synced and services reloaded"

# Reload services without rsync — useful after an emergency edit on EC2.
reload:
	ssh $(HOST) "sudo nginx -t && sudo systemctl reload nginx && sudo fail2ban-client reload"

# Show per-file diff between local and remote.
diff:
	@for f in etc/fail2ban/jail.local etc/fail2ban/jail.d/nginx-404.conf etc/fail2ban/filter.d/nginx-404.conf etc/nginx/snippets/blocklist.conf etc/nginx/sites-enabled/flaz-routing; do \
		echo "=== $$f ==="; \
		ssh $(HOST) "sudo cat /$$f" 2>/dev/null | diff -u $$f - || true; \
	done
