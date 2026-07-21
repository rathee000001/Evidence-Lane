import { t023UiSystemSchemas } from "../contracts/T023UniversalUiSystemSchemas";

export const universalPopupFadeMotion = {
  schemaId: t023UiSystemSchemas.universalPopupFade,
  presenceKey: "universal-governed-popup",
  durationMs: 140,
  reducedMotionDurationMs: 1,
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: {
    duration: 0.14,
    ease: [0.22, 1, 0.36, 1] as [number, number, number, number],
  },
  reducedTransition: {
    duration: 0.001,
    ease: "linear" as const,
  },
};
