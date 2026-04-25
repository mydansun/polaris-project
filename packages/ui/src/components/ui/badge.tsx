import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-md px-2 py-0.5 text-xs font-bold transition-colors",
  {
    variants: {
      variant: {
        default: "border border-success/40 bg-success-light text-success",
        secondary: "border border-border-light bg-surface-alt text-text-muted",
        destructive: "border border-error/40 bg-error-light text-error",
        outline: "border border-border-light text-text-muted",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
