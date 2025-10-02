# CryptoAlerts77 — Custom Flutter client (EN/EL + PIN + FCM-ready)

## Τι είναι έτοιμο
- Base URL: https://crypto-alerts-bot-y4v9.onrender.com
- API KEY: ca77_prod_app2025
- App name (UI): Crypto Coins Alerts77
- Package name (Android): com.cryptoalerts77.app
- Δίγλωσσο UI (English/Greek) με auto-detect από συσκευή.
- PIN linking ροή + αποστολή FCM token (αν βάλεις Firebase).

## Γρήγορα βήματα
1) Δημιούργησε project:
   ```bash
   flutter create cryptoalerts77
   cd cryptoalerts77
   ```
2) **Αντέγραψε** από αυτό το zip:
   - `pubspec.yaml` και όλον τον φάκελο `lib/` (αντικατάσταση)
   - Από `ANDROID_FILES/` στο `android/app/`:
     - `src/main/AndroidManifest.xml` (αντικατάσταση)
     - `src/main/res/values/strings.xml` & `src/main/res/values-el/strings.xml`
     - Όλο το `src/main/res/mipmap-*/` (εικονίδια)
3) Άλλαξε το **applicationId** στο `android/app/build.gradle` (μία φορά):
   - Βρες `applicationId` και κάντο: `applicationId "com.cryptoalerts77.app"`
4) (Προαιρετικό) Firebase push:
   - Βάλε `google-services.json` στο `android/app/`
   - `android/build.gradle`: πρόσθεσε `classpath "com.google.gms:google-services:4.4.2"`
   - `android/app/build.gradle`: `apply plugin: "com.google.gms.google-services"`
5) Τρέξε:
   ```bash
   flutter pub get
   flutter run
   ```
6) Build για Play:
   ```bash
   flutter build appbundle --release
   ```
