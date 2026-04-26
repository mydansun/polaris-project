import * as React from '@theia/core/shared/react';
import { injectable } from '@theia/core/shared/inversify';
import { GettingStartedWidget } from '@theia/getting-started/lib/browser/getting-started-widget';

@injectable()
export class PolarisGettingStartedWidget extends GettingStartedWidget {

    protected override render(): React.ReactNode {
        return (
            <div className="polaris-welcome" style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100%',
                padding: '2rem',
                fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                color: '#516174',
                userSelect: 'none',
            }}>
                <div style={{
                    fontSize: '28px',
                    fontWeight: 700,
                    color: '#1a1f2b',
                    letterSpacing: '-0.5px',
                }}>
                    Polaris IDE
                </div>
                <div style={{
                    marginTop: '12px',
                    fontSize: '14px',
                    lineHeight: 1.6,
                    textAlign: 'center',
                    maxWidth: '400px',
                }}>
                    Your workspace is ready.
                </div>
                <div style={{
                    marginTop: '24px',
                    display: 'flex',
                    gap: '16px',
                    fontSize: '13px',
                }}>
                    <div style={{
                        padding: '8px 16px',
                        borderRadius: '8px',
                        background: '#f4f6f8',
                    }}>
                        Explorer &rarr; browse files
                    </div>
                    <div style={{
                        padding: '8px 16px',
                        borderRadius: '8px',
                        background: '#f4f6f8',
                    }}>
                        Search &rarr; find in workspace
                    </div>
                </div>
            </div>
        );
    }
}
