import { ContainerModule } from '@theia/core/shared/inversify';
import { CommandRegistry } from '@theia/core/lib/common';
import { FrontendApplicationContribution, FrontendApplication } from '@theia/core/lib/browser';
import { GettingStartedWidget } from '@theia/getting-started/lib/browser/getting-started-widget';
import { PolarisGettingStartedWidget } from './polaris-getting-started-widget';

export default new ContainerModule((bind, _unbind, _isBound, rebind) => {
    // Replace the default welcome widget with our custom one.
    bind(PolarisGettingStartedWidget).toSelf();
    rebind(GettingStartedWidget).toService(PolarisGettingStartedWidget);

    // Auto-expand the Explorer sidebar on startup.
    bind(FrontendApplicationContribution).toConstantValue({
        onDidInitializeLayout(_app: FrontendApplication): void {
            // Schedule after the shell is fully rendered.
            setTimeout(() => {
                try {
                    // Reveal the file explorer view container in the left panel.
                    _app.shell.revealWidget('explorer-view-container');
                } catch {
                    // Fallback: ignore if widget not found.
                }
            }, 200);
        }
    });
});
