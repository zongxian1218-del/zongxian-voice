using Microsoft.UI.Xaml;
namespace VideoProbe;
public partial class App : Application
{
    public App() { InitializeComponent(); }
    protected override void OnLaunched(LaunchActivatedEventArgs a)
    { new MainWindow().Activate(); }
}

