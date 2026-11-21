
import { domToPng } from "https://esm.sh/modern-screenshot@4.7.0";

console.log("Screenshot library imported:", typeof domToPng);

async function downloadCard(button) {
    const cardNode = document.getElementById("participant-card");

    if (!cardNode) {
        alert("Participant card not found.");
        return;
    }

    if (typeof domToPng !== "function") {
        alert("Screenshot library failed to initialize.");
        return;
    }

    const originalText = button.textContent;
    const originalDisabled = button.disabled;

    // Save original card styles
    const originalTransform = {
        value: cardNode.style.getPropertyValue("transform"),
        priority: cardNode.style.getPropertyPriority("transform")
    };

    const originalMargin = {
        value: cardNode.style.getPropertyValue("margin-bottom"),
        priority: cardNode.style.getPropertyPriority("margin-bottom")
    };

    // Save original image attributes
    const images = Array.from(cardNode.querySelectorAll("img"));

    const originalImages = images.map(img => ({
        src: img.getAttribute("src"),
        srcset: img.getAttribute("srcset"),
        sizes: img.getAttribute("sizes"),
        crossorigin: img.getAttribute("crossorigin")
    }));

    button.textContent = "Downloading Card...";
    button.disabled = true;

    try {
        // Remove responsive scaling for accurate capture
        cardNode.style.setProperty("transform", "none", "important");
        cardNode.style.setProperty("margin-bottom", "0", "important");

        // Wait for the browser to apply the updated layout
        await new Promise(resolve =>
            requestAnimationFrame(() =>
                requestAnimationFrame(resolve)
            )
        );

        // Convert external images to Base64
        for (let i = 0; i < images.length; i++) {
            const img = images[i];
            const src = originalImages[i].src || img.currentSrc;

            if (!src || src.startsWith("data:") || src.startsWith("blob:")) {
                continue;
            }

            const response = await fetch(src, {
                mode: "cors",
                credentials: "same-origin",
                cache: "force-cache"
            });

            if (!response.ok) {
                throw new Error(`Image loading failed: ${response.status}`);
            }

            const blob = await response.blob();

            const base64 = await new Promise((resolve, reject) => {
                const reader = new FileReader();

                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(
                    new Error("Image conversion failed.")
                );

                reader.readAsDataURL(blob);
            });

            // Prevent responsive srcset from replacing the Base64 image
            img.removeAttribute("srcset");
            img.removeAttribute("sizes");
            img.removeAttribute("crossorigin");
            img.src = base64;

            await img.decode();
        }

        // Wait for fonts and images
        if (document.fonts?.ready) {
            await document.fonts.ready;
        }

        await Promise.all(
            images.map(img =>
                img.complete ? img.decode() : Promise.resolve()
            )
        );

        // Allow final layout and painting to complete
        await new Promise(resolve =>
            requestAnimationFrame(() =>
                requestAnimationFrame(resolve)
            )
        );

        // Capture at 3x resolution.
        // Keep the card's actual CSS width and height.
        const dataUrl = await domToPng(cardNode, {
            scale: 3,
            backgroundColor: "#ffffff"
        });

        if (!dataUrl?.startsWith("data:image/png")) {
            throw new Error("PNG generation failed.");
        }

        // Download PNG
        const link = document.createElement("a");

        link.download =
            `Expo-Card-${button.dataset.name || "Participant"}.png`;

        link.href = dataUrl;

        document.body.appendChild(link);
        link.click();
        link.remove();

        console.log("Participant card downloaded successfully.");

    } catch (error) {
        console.error("Card Download Error:", error);

        alert(
            "Unable to download your card. Please try again.\n" +
            error.message
        );

    } finally {
        // Restore original image attributes
        images.forEach((img, index) => {
            const original = originalImages[index];

            const attributes = {
                src: original.src,
                srcset: original.srcset,
                sizes: original.sizes,
                crossorigin: original.crossorigin
            };

            Object.entries(attributes).forEach(([name, value]) => {
                if (value === null) {
                    img.removeAttribute(name);
                } else {
                    img.setAttribute(name, value);
                }
            });
        });

        // Restore original card styles
        if (originalTransform.value) {
            cardNode.style.setProperty(
                "transform",
                originalTransform.value,
                originalTransform.priority
            );
        } else {
            cardNode.style.removeProperty("transform");
        }

        if (originalMargin.value) {
            cardNode.style.setProperty(
                "margin-bottom",
                originalMargin.value,
                originalMargin.priority
            );
        } else {
            cardNode.style.removeProperty("margin-bottom");
        }

        // Restore download button
        button.textContent = originalText;
        button.disabled = originalDisabled;
    }
}

// Event delegation for dynamically generated cards
document.addEventListener("click", event => {
    const button = event.target.closest("#downloadPng");

    if (!button) return;

    event.preventDefault();
    downloadCard(button);
});
