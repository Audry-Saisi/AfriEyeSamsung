# AfriEye project decisions

## How one screening works (confirmed by the team, 5 November 2026)

1. The community health worker (CHW) takes or uploads a photo of the eye.
2. The AfriEye model scans it on the phone and shows whether it found possible DR.
3. The CHW sees the next step: send a referral, retake the photo, or screen again in 12 months.
4. For a referral, the CHW sends the photo and AI result to the ophthalmologist.
5. The ophthalmologist reviews it and confirms the appointment.
6. The SMS then goes to the patient and chosen family members.

## Hardware and product scope (confirmed by the team, 5 November 2026)

- AfriEye will use a retinal adapter that is already on the market. We will not build our own hardware.
- AfriEye will build the AI model that gives the first result, plus dashboards for clinicians and patients.
- Which adapter to use, and who gives any dilating drops, is still to be decided.

## Target users and core statements (confirmed by the team, 5 November 2026)

- **Who we help:** older adults with diabetes in communities far from eye clinics.
- **Who uses it:** community health workers who already visit those communities.
- **Problem statement:** Older adults with diabetes can lose their sight without early warning, because eye checks and specialists are far from where they live.
- **Solution statement:** A CHW uses a phone with a retinal adapter to screen them at home, and AfriEye flags who needs an ophthalmologist, even without internet.

## How we choose the final model (5 November 2026)

Only models that can run offline on a phone qualify. The highest validation AUC wins. If models are within 0.005, the fewest severe photos missed on validation wins, then the smallest model. Test photos are never used to choose.
