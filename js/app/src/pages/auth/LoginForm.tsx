import { css } from "@emotion/react";
import { useCallback, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { useNavigate } from "react-router";

import {
  Alert,
  Button,
  Flex,
  Form,
  Icon,
  Icons,
  Input,
  Label,
  Link,
  TextField,
  View,
} from "@phoenix/components";
import {
  assignAppRelativeLocation,
  createRedirectUrlWithReturn,
  getReturnUrl,
  isServerOwnedPath,
  prependBasename,
} from "@phoenix/utils/routingUtils";

type LoginFormParams = {
  email: string;
  password: string;
};

type LoginFormProps = {
  initialError: string | null;
  /**
   * Callback function called when the form is submitted
   */
  onSubmit?: () => void;
};
export function LoginForm(props: LoginFormProps) {
  const navigate = useNavigate();
  const { initialError, onSubmit: propsOnSubmit } = props;
  const [error, setError] = useState<string | null>(initialError);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const onSubmit = useCallback(
    async (params: LoginFormParams) => {
      propsOnSubmit?.();
      setError(null);
      setIsLoading(true);

      // Sanitize email by trimming whitespace and converting to lowercase
      const sanitizedParams = {
        ...params,
        email: params.email.trim().toLowerCase(),
      };

      let requiresGroupSelection = false;
      try {
        const response = await fetch(prependBasename("/auth/login"), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(sanitizedParams),
        });
        if (!response.ok) {
          const errorMessage =
            response.status === 429
              ? "Too many requests. Please try again later."
              : "Invalid login";
          setError(errorMessage);
          return;
        }
        // A multi-group user gets a 200 + body instead of the usual empty
        // 204 -- see _create_auth_response in phoenix.server.api.routers.auth.
        // Login itself already succeeded at this point (cookies are set),
        // so a body-parsing hiccup here must not surface as "Invalid
        // login" -- it's isolated from the network try/catch above.
        if (response.status !== 204) {
          try {
            const payload = (await response.json()) as {
              requiresGroupSelection?: boolean;
            };
            requiresGroupSelection = payload.requiresGroupSelection === true;
          } catch {
            // Fall through to normal post-login navigation.
          }
        }
      } catch (_error) {
        setError("Invalid login");
        return;
      } finally {
        setIsLoading(() => false);
      }
      if (requiresGroupSelection) {
        navigate(createRedirectUrlWithReturn({ path: "/login/choose-group" }));
        return;
      }
      const returnUrl = getReturnUrl();
      if (isServerOwnedPath(returnUrl)) {
        assignAppRelativeLocation(returnUrl);
        return;
      }
      navigate(returnUrl);
    },
    [navigate, propsOnSubmit, setError]
  );
  const { control, handleSubmit } = useForm<LoginFormParams>({
    defaultValues: { email: "", password: "" },
  });
  return (
    <>
      {error ? (
        <View paddingBottom="size-100">
          <Alert variant="danger">{error}</Alert>{" "}
        </View>
      ) : null}
      <Form onSubmit={handleSubmit(onSubmit)}>
        <Flex direction="column" gap="size-200">
          <Flex direction="column" gap="size-100">
            <Controller
              name="email"
              control={control}
              render={({ field: { onChange, value, onBlur } }) => (
                <TextField
                  name="email"
                  isRequired
                  type="email"
                  autoFocus
                  onChange={onChange}
                  onBlur={onBlur}
                  value={value}
                  autoComplete="email"
                  onKeyDown={(e) => {
                    // Prevent form submission on Enter - user likely wants to tab to password
                    if (e.key === "Enter") {
                      e.preventDefault();
                    }
                  }}
                >
                  <Label>Email</Label>
                  <Input placeholder="your email address" />
                </TextField>
              )}
            />
            <div
              css={css`
                position: relative;
                .link-container {
                  position: absolute;
                  float: right;
                  right: 0;
                  top: var(--global-dimension-size-50);
                  font-size: 12px;
                }
              `}
            >
              <Controller
                name="password"
                control={control}
                render={({ field: { onChange, value } }) => (
                  <TextField
                    name="password"
                    type="password"
                    isRequired
                    onChange={onChange}
                    value={value}
                    autoComplete="current-password"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void handleSubmit(onSubmit)();
                      }
                    }}
                  >
                    <Label>Password</Label>
                    <Input placeholder="your password" />
                  </TextField>
                )}
              />
              {window.Config.passwordResetEmailEnabled ? (
                <Link id="forgot-password-link" to="/forgot-password">
                  Forgot your password?
                </Link>
              ) : null}
            </div>
          </Flex>
          <Button
            variant="primary"
            type="submit"
            isDisabled={isLoading}
            leadingVisual={
              isLoading ? <Icon svg={<Icons.Loading />} /> : undefined
            }
          >
            {isLoading ? "Logging In" : "Log In"}
          </Button>
        </Flex>
      </Form>
    </>
  );
}
