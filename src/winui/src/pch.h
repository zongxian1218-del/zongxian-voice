// pch.h —— 预编译头
//
// WinUI 3 + C++/WinRT 的头文件量非常大，不预编译的话每次全量构建要几分钟。
// 这里只放「几乎每个 .cpp 都会用到」的头，别往里加业务相关的东西。
#pragma once

#include <windows.h>

// C++/WinRT 基础
#include <unknwn.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.System.h>
#include <winrt/Windows.UI.h>

// WinUI 3 控件与窗口
#include <winrt/Microsoft.UI.h>
#include <winrt/Microsoft.UI.Xaml.h>
#include <winrt/Microsoft.UI.Xaml.Controls.h>
#include <winrt/Microsoft.UI.Xaml.Controls.Primitives.h>
#include <winrt/Microsoft.UI.Xaml.Data.h>
#include <winrt/Microsoft.UI.Xaml.Interop.h>
#include <winrt/Microsoft.UI.Xaml.Markup.h>
#include <winrt/Microsoft.UI.Xaml.Media.h>
#include <winrt/Microsoft.UI.Xaml.Navigation.h>
#include <winrt/Microsoft.UI.Xaml.Shapes.h>

// 标准库
#include <string>
#include <vector>
#include <memory>
#include <functional>
