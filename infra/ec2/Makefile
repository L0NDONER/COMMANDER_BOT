# Host-level config sync for the EC2 box (nginx, fail2ban).
# App code goes via GitHub Actions; this is the parallel path for everything
# outside the docker-mounted repo. Files under ./etc/ mirror the remote /etc/.

HOST  := aws
LOCAL := etc/
RSYNC := rsync -av --rsync-path="sudo rsync" --no-owner --no-group

.PHONY: help pull plan sync reload diff
.DEFAULT_GOAL := help

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-8s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

pull: ## Pull current EC2 state down (run after manual edits / certbot renewals).
	rsync -av $(HOST):/etc/fail2ban/jail.local              etc/fail2ban/jail.local
	rsync -av $(HOST):/etc/fail2ban/jail.d/nginx-404.conf   etc/fail2ban/jail.d/nginx-404.conf
	rsync -av $(HOST):/etc/fail2ban/filter.d/nginx-404.conf etc/fail2ban/filter.d/nginx-404.conf
	rsync -av $(HOST):/etc/nginx/snippets/blocklist.conf    etc/nginx/snippets/blocklist.conf
	rsync -av $(HOST):/etc/nginx/sites-enabled/flaz-routing etc/nginx/sites-enabled/flaz-routing

plan: ## Dry-run: show what sync would change without writing anything.
	$(RSYNC) --dry-run --itemize-changes $(LOCAL) $(HOST):/etc/

sync: ## Push local -> EC2, validate nginx, reload nginx + fail2ban.
	$(RSYNC) $(LOCAL) $(HOST):/etc/
	ssh $(HOST) "sudo nginx -t && sudo systemctl reload nginx && sudo fail2ban-client reload"
	@echo "OK infra synced and services reloaded"

reload: ## Reload services without rsync (after an emergency edit on EC2).
	ssh $(HOST) "sudo nginx -t && sudo systemctl reload nginx && sudo fail2ban-client reload"

diff: ## Show per-file diff between local and remote.
	@for f in etc/fail2ban/jail.local etc/fail2ban/jail.d/nginx-404.conf etc/fail2ban/filter.d/nginx-404.conf etc/nginx/snippets/blocklist.conf etc/nginx/sites-enabled/flaz-routing; do \
		echo "=== $$f ==="; \
		ssh $(HOST) "sudo cat /$$f" 2>/dev/null | diff -u $$f - || true; \
	done
