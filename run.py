"""PyInstaller entry point — uses absolute imports (relative imports break when frozen)."""

# Explicit imports ensure PyInstaller bundles all language adapters and detectors.
import ut_analyzer.adapters.cpp.adapter  # noqa: F401
import ut_analyzer.adapters.cpp.build_systems  # noqa: F401
import ut_analyzer.adapters.cpp.frameworks  # noqa: F401
import ut_analyzer.adapters.csharp.adapter  # noqa: F401
import ut_analyzer.adapters.go.adapter  # noqa: F401
import ut_analyzer.adapters.java.adapter  # noqa: F401
import ut_analyzer.adapters.javascript.adapter  # noqa: F401
import ut_analyzer.adapters.python.adapter  # noqa: F401
import ut_analyzer.core.detection.framework_hints  # noqa: F401
import ut_analyzer.core.detection.test_content_sampler  # noqa: F401
import ut_analyzer.core.discovery.test_file_resolver  # noqa: F401

from ut_analyzer.main import main

if __name__ == "__main__":
    raise SystemExit(main())
