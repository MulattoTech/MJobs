type CredentialConnectionFeedbackProps = {
  status: string;
  code: string | null;
};

type Feedback = { role: 'status' | 'alert'; message: string };

function feedbackFor(status: string, code: string | null): Feedback {
  if (status === 'CONNECTED') {
    return {
      role: 'status',
      message: 'Provider authentication succeeded. No model inference was run.',
    };
  }

  if (status !== 'FAILED') {
    return {
      role: 'alert',
      message: 'The provider check could not be completed. Try again. No provider details were shown.',
    };
  }

  switch (code) {
    case 'CREDENTIAL_REJECTED':
      return {
        role: 'alert',
        message: 'The provider rejected this credential. Check that the saved key is current and has the required access, then rotate it and test again.',
      };
    case 'CREDENTIAL_PROVIDER_TIMEOUT':
      return {
        role: 'alert',
        message: 'The provider did not respond before the connection check timed out. Try again in a moment; this does not mean the credential was rejected.',
      };
    case 'CREDENTIAL_PROVIDER_UNAVAILABLE':
      return {
        role: 'alert',
        message: 'The provider could not be reached or did not complete the check. Check your connection or provider status, then try again.',
      };
    case 'CREDENTIAL_PROVIDER_UNSUPPORTED':
      return {
        role: 'alert',
        message: 'This provider is not supported for credential checks. Choose a supported provider and try again.',
      };
    default:
      return {
        role: 'alert',
        message: 'The provider check could not be completed. Try again. No provider details were shown.',
      };
  }
}

export function CredentialConnectionFeedback({
  status,
  code,
}: CredentialConnectionFeedbackProps) {
  const feedback = feedbackFor(status, code);
  return (
    <p role={feedback.role} aria-live={feedback.role === 'status' ? 'polite' : 'assertive'}>
      {feedback.message}
    </p>
  );
}
