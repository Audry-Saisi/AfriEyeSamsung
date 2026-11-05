# Step 4: testing AfriEye on real adapter photos

**Why this matters:** every score so far comes from retinal camera photos. No photo from a phone adapter has been tested yet, and accuracy may drop. This test answers the judge's feasibility question with evidence.

**What we need:** one eye specialist, 2 adapters, 30 to 50 adults with diabetes, and ethics approval before any real patient is photographed.

## Before starting

Prices and availability below come from our earlier research. Confirm them with each seller.

| Device | Approx. cost | Drops to widen the pupil? | Use in this test |
|---|---|---|---|
| D-EYE | $390 to $435 | Works better with them: about 5 to 8° view without, 20 to 25° with | Low-cost option |
| iNview | About £700 | Effectively yes | Low-cost option |
| oDocs visoScope | About £260 | Effectively yes | Backup low-cost option |
| Remidio FOP NM | About £4,600 | No | Comparison if a clinic can lend one |
| Volk Pictor | About £4,400 | No | Comparison if a clinic can lend one |

**Decisions to make first**

1. **Who gives drops?** A CHW may not be allowed to. Ask the clinician and the county health office.
2. **Which 2 adapters?** Suggested: D-EYE and iNview, compared with a standard retinal camera at the clinic.
3. **Which phones fit?** Check each adapter against the phones CHWs actually use.
4. **Who approves it?** Apply to a recognised ethics committee in Kenya. The eye specialist or a partner university can advise which one.

## How to run it

1. Explain the test to each person and get written consent.
2. Take one photo of each eye with each adapter, and one with the clinic's retinal camera.
3. Note whether each photo needed drops, how many attempts it took, and how long it took.
4. Remove names from the photos, and give each person a study number.
5. The eye specialist grades every photo from 0 to 4, without seeing the AI result. The retinal camera grade is the reference.
6. Any patient found with possible DR goes to the clinic for normal care. The AI result must not delay this.

## Files to prepare

Put the photos in `data/new_photos/adapter_test_1/images/`, and create `labels.csv`:

```
filename,grade,patient_id,device,gradable
p001_right_dEYE.jpg,2,1,D-EYE,1
p001_right_iNview.jpg,2,1,iNview,1
```

`grade` should be the specialist's grade for that eye from the retinal camera photo. Set `gradable` to 0 if the specialist couldn't read that adapter photo.

Then run `notebooks/11_test_on_new_photos.ipynb`. Don't change the model's cut-off to suit these photos.

## How to read the result

Agree these targets with the eye specialist **before** seeing the results. Suggested starting points:

| Measure | Suggested target | If it falls short |
|---|---|---|
| Photos the specialist can grade | At least 80% | Improve the training or the adapter choice, or send more people straight to the clinic |
| Referable eyes caught (sensitivity) | At least 85% | Retrain with adapter photos |
| Severe eyes missed | None | Don't use the model on that adapter |
| Time per patient | Under 10 minutes | Simplify the steps |

With only 30 to 50 people, the result is an early signal and can't prove accuracy. If the model falls short, adapter photos can be used to retrain it, but it must then be tested on different patients.

## Research demo

AfriEye isn't a medical device. Any real clinical use needs ethics approval, clinical validation, and approval from Kenya's regulator. The team should confirm the current requirements.
