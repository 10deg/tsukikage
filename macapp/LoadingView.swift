import Cocoa

final class LoadingView: NSView {
    private let titleLabel = NSTextField(labelWithString: "Tsukikage")
    private let statusLabel = NSTextField(labelWithString: "正在启动…")
    private let spinner = NSProgressIndicator()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor(calibratedWhite: 0.028, alpha: 1).cgColor

        titleLabel.font = NSFont.systemFont(ofSize: 30, weight: .semibold)
        titleLabel.textColor = NSColor(calibratedWhite: 0.94, alpha: 1)
        titleLabel.alignment = .center

        statusLabel.font = NSFont.systemFont(ofSize: 13, weight: .regular)
        statusLabel.textColor = NSColor(calibratedWhite: 0.68, alpha: 1)
        statusLabel.alignment = .center
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.maximumNumberOfLines = 0

        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.startAnimation(nil)

        let stack = NSStackView(views: [titleLabel, statusLabel, spinner])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)

        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, constant: -80),
            statusLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 680),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setStatus(_ text: String) {
        statusLabel.stringValue = text
    }
}
