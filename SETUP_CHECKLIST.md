# Five-Minute Setup Checklist

- [ ] Create an empty GitHub repository called `free-swing-paper-bot`.
- [ ] Upload the complete project into that repository.
- [ ] Create a Discord server and a channel named `swing-paper-alerts`.
- [ ] Create and copy the channel webhook URL.
- [ ] Add the GitHub Actions secret `DISCORD_WEBHOOK_URL`.
- [ ] Give GitHub Actions read-and-write workflow permission.
- [ ] Run the `test-discord` workflow manually.
- [ ] Run the `refresh` workflow manually.
- [ ] Confirm that `state/runtime.json` shows an eligible Daily pool.
- [ ] Leave the scheduled workflow enabled.
