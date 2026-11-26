# sitecrew-testers

Emulator CI for SiteCrew AI (com.sitecrew.ai): device kill leg + UI tests on GitHub free runners.

- kill-leg.yml: builds the x86_64 APK from the committed payload, boots an emulator,
  runs the P1.2-D kill/relaunch cycles (event-driven adb only), uploads evidence.
- No secrets, no model keys. The app payload mirrors the deployed phone build.

