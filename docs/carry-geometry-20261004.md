# Carry geometry correction — 2026-10-04

Two geometry errors were independently reproduced with failing tests:

1. The spin axis stayed fixed when horizontal launch direction changed. The
   same shot aimed sideways then changed flight shape in still air. The spin
   axis now rotates with launch azimuth; its tilt/sign convention is retained.
2. Carry was the landing point's target-axis projection (`x`), not the horizontal
   launch-to-landing distance. Carry is now `hypot(x, y)`. Lateral distance and
   trajectory coordinates retain their meanings.

The flat-distance definition follows the [Trackman carry definition](https://support.trackmangolf.com/hc/en-us/articles/39726543090971-Parameters-Carry-Tee-to-Green).
No aerodynamic coefficients, measured angles, measured speeds, spin assumptions,
or environmental parameters are fitted or changed.

Yaw-rotation tests cover ±10°/25° with draw, straight and fade spin axes. The
24-shot committed Trackman fixture remains within all existing error budgets.
Its carry RMSE is 2.29 yd; apex RMSE is 2.58 ft.

On 45 saved October 3 shots the median carry change is +0.37 m, maximum +2.87 m.
For the 37 robust Toptracer pairs excluding the separately identified possible
topped TT44 shot, carry MAE changes only from 7.48 to 7.32 m. Including TT44,
38-pair MAE is 9.36 to 9.21 m. This is a geometry fix, not a complete accuracy fix.
Some club groups improve and others do not. Historical shots are not rewritten.

The pre-existing fallback spin remains explicitly unmeasured. The new spin
safeguard does not promote candidates; no new standard-spin table or carry
scale factor is introduced. The remaining discrepancy requires reliable spin
evidence and controlled comparison conditions.
