import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cn } from "../../lib/utils";

/**
 * Sheet — a Radix-Dialog-backed side drawer.
 *
 * Same semantics as Dialog (controlled via `open` / `onOpenChange`), but
 * the content slides in from a chosen side instead of zooming to center.
 * Uses tw-animate-css slide utilities, which `apps/web` already imports.
 */

const Sheet = DialogPrimitive.Root;
const SheetTrigger = DialogPrimitive.Trigger;
const SheetPortal = DialogPrimitive.Portal;
const SheetClose = DialogPrimitive.Close;

const SheetOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-black/40 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className,
    )}
    {...props}
  />
));
SheetOverlay.displayName = DialogPrimitive.Overlay.displayName;

type SheetSide = "left" | "right" | "top" | "bottom";

type SheetContentProps = React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
  /** Which edge the sheet slides in from.  Defaults to `right`. */
  side?: SheetSide;
};

const SIDE_CLASS: Record<SheetSide, string> = {
  left:   "inset-y-0 left-0   h-full w-80 border-r data-[state=closed]:slide-out-to-left  data-[state=open]:slide-in-from-left",
  right:  "inset-y-0 right-0  h-full w-80 border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
  top:    "inset-x-0 top-0    h-80 w-full border-b data-[state=closed]:slide-out-to-top   data-[state=open]:slide-in-from-top",
  bottom: "inset-x-0 bottom-0 h-80 w-full border-t data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom",
};

const SheetContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  SheetContentProps
>(({ className, children, side = "right", ...props }, ref) => (
  <SheetPortal>
    <SheetOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed z-50 flex flex-col border-border-light bg-white shadow-xl transition ease-out data-[state=closed]:duration-200 data-[state=open]:duration-300 data-[state=open]:animate-in data-[state=closed]:animate-out",
        SIDE_CLASS[side],
        className,
      )}
      {...props}
    >
      {children}
    </DialogPrimitive.Content>
  </SheetPortal>
));
SheetContent.displayName = DialogPrimitive.Content.displayName;

function SheetHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex flex-col gap-1.5 border-b border-border-light p-4 text-left", className)}
      {...props}
    />
  );
}
SheetHeader.displayName = "SheetHeader";

function SheetFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex flex-row justify-end gap-2 border-t border-border-light p-4", className)}
      {...props}
    />
  );
}
SheetFooter.displayName = "SheetFooter";

const SheetTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn("text-base font-semibold leading-none text-text-primary", className)}
    {...props}
  />
));
SheetTitle.displayName = DialogPrimitive.Title.displayName;

const SheetDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-xs text-text-muted", className)}
    {...props}
  />
));
SheetDescription.displayName = DialogPrimitive.Description.displayName;

export {
  Sheet,
  SheetTrigger,
  SheetPortal,
  SheetClose,
  SheetOverlay,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
};
