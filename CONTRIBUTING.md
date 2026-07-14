# Contributing to Knowledge-Base-Builder

Thank you for your interest in contributing to Knowledge-Base-Builder! This document provides guidelines and instructions for contributing code, documentation, bug reports, and feature requests.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Development Setup](#development-setup)
- [Contribution Workflow](#contribution-workflow)
- [Coding Standards](#coding-standards)
- [Testing Guidelines](#testing-guidelines)
- [Documentation Standards](#documentation-standards)
- [Reporting Issues](#reporting-issues)
- [Feature Requests](#feature-requests)
- [Pull Request Process](#pull-request-process)
- [Recognition](#recognition)

## Code of Conduct

### Our Pledge

In the interest of fostering an open and welcoming environment, we as contributors and maintainers pledge to making participation in our project and our community a harassment-free experience for everyone, regardless of age, body size, disability, ethnicity, gender identity and expression, level of experience, nationality, personal appearance, race, religion, or sexual identity and orientation.

### Our Standards

Examples of behavior that contributes to creating a positive environment include:
- Using welcoming and inclusive language
- Being respectful of differing viewpoints and experiences
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

Examples of unacceptable behavior include:
- The use of sexualized language or imagery
- Trolling, insulting/derogatory comments, and personal or political attacks
- Public or private harassment
- Publishing others' private information without explicit permission
- Other unethical or unprofessional conduct

### Responsibility

Project maintainers are responsible for clarifying the standards of acceptable behavior and are expected to take appropriate and fair corrective action in response to any instances of unacceptable behavior.

### Scope

This Code of Conduct applies both within project spaces and in public spaces when an individual is representing the project or its community.

### Enforcement

Instances of abusive, harassing, or otherwise unacceptable behavior may be reported by contacting the project team. All complaints will be reviewed and investigated and will result in a response that is deemed necessary and appropriate to the circumstances. The project team is obligated to maintain confidentiality with regard to the reporter of an incident.

## Ways to Contribute

### Code Contributions

- Bug fixes
- New features
- Performance improvements
- Refactoring
- Test improvements

### Documentation Contributions

- Improving existing documentation
- Adding examples and tutorials
- Translating documentation
- Fixing typos and errors
- Adding diagrams and illustrations

### Community Contributions

- Answering questions in issues
- Helping other users
- Sharing your use cases
- Providing feedback on features
- Testing pre-release versions

### Other Contributions

- Design and UX improvements
- Accessibility improvements
- Internationalization
- Security audits
- Performance profiling

## Development Setup

### Prerequisites

- Python 3.8 or higher
- Git
- Virtual environment (recommended)

### Initial Setup

```bash
# Fork the repository on GitHub
# Clone your fork
git clone https://github.com/realdocfx/knowledge_base_builder.git
cd knowledge_base_builder

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install development dependencies
pip install -e ".[dev]"

# Run tests to verify setup
pytest
```

### Development Workflow

1. Create a new branch for your work
2. Make your changes
3. Write/update tests
4. Format code with black and isort
5. Type check with mypy
6. Run tests
7. Commit changes
8. Push to your fork
9. Create pull request

## Contribution Workflow

### Branch Naming

Use descriptive branch names:
- `feature/your-feature-name`
- `fix/your-bug-fix`
- `docs/your-documentation-update`
- `refactor/your-refactoring`

### Commit Messages

Follow conventional commits format:
```
type(scope): subject

body

footer
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes
- `refactor`: Code refactoring
- `test`: Test changes
- `chore`: Maintenance tasks

Examples:
```
feat(engine): add format filtering to download_item

- Add formats parameter to download_item method
- Update size estimation to support format filtering
- Add tests for format filtering logic

Closes #123
```

### Pull Request Process

1. **Update documentation** if your changes affect user-facing behavior
2. **Add tests** for new functionality or bug fixes
3. **Ensure all tests pass** before submitting
4. **Update CHANGELOG.md** if applicable
5. **Reference related issues** in your pull request
6. **Wait for code review** and address feedback

## Coding Standards

### Code Style

- Follow PEP 8 guidelines
- Use black for code formatting
- Use isort for import sorting
- Use mypy for type checking
- Maximum line length: 88 characters

### Documentation

- Add docstrings to all public functions and classes
- Use Google-style docstrings
- Include type hints for all functions
- Add inline comments for complex logic

### Testing

- Write tests for all new functionality
- Maintain test coverage above 80%
- Use pytest for testing
- Use fixtures for common test setup
- Test edge cases and error conditions

### Example Code

```python
from typing import Optional, List

def download_item(
    identifier: str,
    destdir: str,
    formats: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Downloads an item with built-in concurrency and checksum validation.
    
    Args:
        identifier: Archive.org item identifier
        destdir: Destination directory path
        formats: List of formats to download (None for all)
        
    Returns:
        Dict with download statistics including files_downloaded, 
        bytes_downloaded, and errors.
        
    Raises:
        Exception: If download fails after all retries
        
    Example:
        >>> engine.download_item("item-id", "/path/to/dir")
        {'files_downloaded': 5, 'bytes_downloaded': 1024000, 'errors': []}
    """
    # Implementation here
    pass
```

## Testing Guidelines

### Test Structure

Organize tests by module:
```
tests/
├── test_robustness.py
├── test_cli.py
└── conftest.py
```

### Writing Tests

```python
import pytest
from knowledge_base_builder import UsbBucket

def test_format_bytes():
    """Test byte formatting function."""
    assert UsbBucket._format_bytes(1024) == "1.0 KB"
    assert UsbBucket._format_bytes(1024 * 1024) == "1.0 MB"

def test_check_capacity_insufficient(tmp_path):
    """Test capacity checking with insufficient space."""
    bucket = UsbBucket(str(tmp_path))
    bucket.initialize()
    with pytest.raises(MemoryError):
        bucket.check_capacity(10**12)
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_robustness.py

# Run with coverage
pytest --cov=src/knowledge_base_builder --cov-report=html

# Run with verbose output
pytest -v
```

## Documentation Standards

### Documentation Files

- **README.md**: User-facing quick start guide
- **ARCHITECTURE.md**: Technical architecture documentation
- **API_REFERENCE.md**: API documentation
- **DEVELOPER_GUIDE.md**: Developer guide
- **TROUBLESHOOTING.md**: Troubleshooting guide
- **FAQ.md**: Frequently asked questions
- **CONTRIBUTING.md**: This file

### Documentation Style

- Use clear, concise language
- Provide examples for all user-facing features
- Include code blocks with syntax highlighting
- Use consistent formatting
- Cross-reference related documentation
- Update documentation with code changes

### Docstring Style

Use Google-style docstrings:

```python
def function_name(param1: str, param2: int) -> bool:
    """Brief description of function.
    
    Longer description if needed.
    
    Args:
        param1: Description of param1
        param2: Description of param2
        
    Returns:
        Description of return value
        
    Raises:
        ExceptionType: Description of when this is raised
        
    Example:
        >>> function_name("test", 42)
        True
    """
    pass
```

## Reporting Issues

### Before Reporting

1. Search existing issues to avoid duplicates
2. Check if the issue is already fixed in the latest version
3. Gather relevant information:
   - Knowledge-Base-Builder version
   - Python version
   - Operating system
   - Error messages and stack traces
   - Steps to reproduce
   - Expected vs actual behavior

### Issue Template

Use the GitHub issue template if available:

```markdown
**Description**
A clear description of what the issue is.

**Steps to Reproduce**
1. Step one
2. Step two
3. Step three

**Expected Behavior**
What you expected to happen.

**Actual Behavior**
What actually happened.

**Environment**
- Knowledge-Base-Builder version: X.Y.Z
- Python version: X.Y.Z
- Operating system: [Windows/macOS/Linux]

**Additional Context**
Logs, screenshots, or other relevant information.
```

### Bug Reports

For bug reports, include:
- Clear description of the bug
- Steps to reproduce
- Expected vs actual behavior
- Environment details
- Relevant logs or error messages
- Possible solutions (if known)

## Feature Requests

### Before Requesting

1. Search existing feature requests
2. Check if the feature fits the project scope
3. Consider if you can implement it yourself

### Feature Request Template

```markdown
**Feature Description**
A clear description of the feature.

**Use Case**
Describe the use case for this feature.
Why would this be useful?

**Proposed Solution**
Describe how you envision this feature working.

**Alternatives Considered**
Describe any alternative solutions or features considered.

**Additional Context**
Any other context, mockups, or examples.
```

## Pull Request Process

### Before Submitting

1. **Code quality**
   - All tests pass
   - Code formatted with black
   - Imports sorted with isort
   - Type checking passes with mypy
   - Code coverage maintained

2. **Documentation**
   - Updated relevant documentation
   - Added docstrings to new functions
   - Updated CHANGELOG.md

3. **Testing**
   - Tests for new functionality
   - Tests for bug fixes
   - Edge cases covered

### Pull Request Template

```markdown
**Description**
Brief description of changes.

**Type of Change**
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

**Testing**
- [ ] Tests added/updated
- [ ] All tests pass
- [ ] Manual testing performed

**Documentation**
- [ ] Documentation updated
- [ ] CHANGELOG.md updated

**Related Issues**
Closes #123
Related to #456
```

### Review Process

1. Automated checks must pass
2. Code review by maintainers
3. Address review feedback
4. Approval from at least one maintainer
5. Merge when ready

### After Merge

- Delete your branch (if desired)
- Update your local repository
- Celebrate your contribution! 🎉

## Recognition

### Contributors

All contributors are recognized in:
- CONTRIBUTORS.md file
- Release notes
- Project documentation

### Attribution

Your contributions will be:
- Listed in the project's contributor list
- Attributed in relevant changelog entries
- Recognized in release announcements

### Becoming a Maintainer

Active contributors may be invited to become maintainers based on:
- Consistent quality contributions
- Understanding of the codebase
- Participation in code reviews
- Helpfulness to other contributors
- Alignment with project goals

## Getting Help

### Questions

If you have questions about contributing:
- Check existing documentation
- Search existing issues and discussions
- Create an issue with the "question" label
- Join community discussions

### Support

For help with contributions:
- Ask in GitHub discussions
- Contact maintainers via GitHub issues
- Refer to DEVELOPER_GUIDE.md for technical guidance

## License

By contributing to Knowledge-Base-Builder, you agree that your contributions will be licensed under the CC0-1.0 license.

## Thank You

Thank you for contributing to Knowledge-Base-Builder! Your contributions help make this project better for everyone.
