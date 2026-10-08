import React from 'react';

export const SENSITIVE_ANSWER_CATEGORIES = [
  { key:'RACE_ETHNICITY', label:'Race and ethnicity' },
  { key:'RELIGION', label:'Religion' },
  { key:'SEX_GENDER', label:'Sex and gender' },
  { key:'DISABILITY', label:'Disability' },
  { key:'VETERAN_STATUS', label:'Veteran status' },
  { key:'OTHER_VOLUNTARY_DEMOGRAPHIC', label:'Other voluntary demographics' },
] as const;

export type SensitiveAnswerCategory = typeof SENSITIVE_ANSWER_CATEGORIES[number]['key'];
export type SensitiveAnswerMode = 'PREFER_NOT_TO_ANSWER' | 'USE_CONFIGURED_RESPONSE' | 'ALWAYS_REQUIRE_REVIEW';

export type SensitiveAnswerCategoryPolicy = {
  mode: SensitiveAnswerMode;
  response?: string;
};

export type SensitiveAnswerPolicyValue = Partial<Record<SensitiveAnswerCategory, SensitiveAnswerCategoryPolicy>>;

export type SensitiveAnswerPolicyProps = {
  value: SensitiveAnswerPolicyValue;
  onValueChange: (value: SensitiveAnswerPolicyValue) => void;
  disabled?: boolean;
};

const MODES: ReadonlyArray<{ value:SensitiveAnswerMode; label:string }> = [
  { value:'PREFER_NOT_TO_ANSWER', label:'Prefer not to answer' },
  { value:'USE_CONFIGURED_RESPONSE', label:'Use configured response' },
  { value:'ALWAYS_REQUIRE_REVIEW', label:'Always require review' },
];

export function SensitiveAnswerPolicy({ value, onValueChange, disabled = false }:SensitiveAnswerPolicyProps) {
  const updateCategory = (category:SensitiveAnswerCategory, policy:SensitiveAnswerCategoryPolicy) => {
    onValueChange({ ...value, [category]:policy });
  };

  return <section aria-labelledby="sensitive-answer-policy-heading">
    <h3 id="sensitive-answer-policy-heading">Sensitive answer preferences</h3>
    <p>Any proposed value requires review for each application and never enters reusable Answer Bank suggestions.</p>
    {SENSITIVE_ANSWER_CATEGORIES.map(({ key, label }) => {
      const policy = value[key];
      const mode = policy?.mode ?? 'ALWAYS_REQUIRE_REVIEW';
      return <fieldset key={key}>
        <legend>{label}</legend>
        <label>
          Response policy for {label}
          <select
            aria-label={`${label} response policy`}
            value={mode}
            disabled={disabled}
            onChange={event => updateCategory(key, { mode:event.currentTarget.value as SensitiveAnswerMode })}
          >
            {MODES.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        {mode === 'USE_CONFIGURED_RESPONSE' && <label>
          {label} configured response
          <input
            aria-label={`${label} configured response`}
            type="text"
            autoComplete="off"
            value={policy?.response ?? ''}
            disabled={disabled}
            onChange={event => updateCategory(key, { mode:'USE_CONFIGURED_RESPONSE', response:event.currentTarget.value })}
          />
        </label>}
      </fieldset>;
    })}
  </section>;
}
