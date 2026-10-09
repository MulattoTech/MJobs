/**
 * Public application-review entry point for the shared dynamic mock ATS form.
 * Keep the dynamic field behavior in one component so review gates cannot drift.
 */
export { DynamicApplicationForm as ApplicationDynamicFieldReview } from './dynamic-application-form';
export type {
  DynamicApplicationAnswer,
  DynamicApplicationAnswers,
  DynamicApplicationContinuePayload,
  DynamicApplicationField,
  DynamicApplicationFieldKind,
  DynamicApplicationFormProps,
  DynamicApplicationFormState,
  DynamicApplicationRestoredDraft,
} from './dynamic-application-form';
