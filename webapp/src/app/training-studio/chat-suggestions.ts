export interface ChatSuggestion {
  header: string;
  desc: string;
  icon: string;
  color: 'purple' | 'pink' | 'cyan' | 'green' | 'orange';
  prompt: string;
}

export const CHAT_SUGGESTIONS: ChatSuggestion[] = [
  {
    header: 'Structured Training Deck',
    desc: 'Instructional modules & assessments',
    icon: '🎓',
    color: 'purple',
    prompt: `Create a professional, learner-friendly training presentation that organizes learning content, practical activities, and assessments into a clear and logical learning journey. Prompt:   Act as an experienced Learning & Development specialist and instructional designer. Using the provided learning outcomes, modules, exercises, examples, participant roles, and supporting resources, create a professional and structured training presentation suitable for a diverse audience, including both technical and non-technical users.   Organize the presentation into a clear and logical sequence. Include:   - Title and introduction - Training purpose and scope - Target audience - Learning objectives and expected outcomes - Module-wise lesson content - Key concepts and explanations - Relevant examples and practical scenarios - Guided exercises and interactive activities - Knowledge checks, quizzes, and assessments - Key takeaways and summary - Additional resources and reference links - Recommended next steps   For each slide, provide:   - A concise and descriptive slide title - Clear, presentation-ready content - Relevant examples or exercises, where applicable - Speaker notes or facilitation guidance, when useful - Recommendations for visuals, diagrams, or illustrations that support understanding   Use consistent content labels or tags such as:   - “Key Concept” for essential information - “Example” for practical demonstrations - “Activity” for learner participation - “To Review” for topics requiring further attention - “Quiz” for knowledge checks - “Resource” for supporting references   Maintain a formal, clear, inclusive, organized, and learner-friendly tone. Use plain language, define technical terms when necessary, and avoid assumptions about the audience’s prior knowledge or experience. Ensure the content is accessible and relevant to users with different roles, skill levels, and professional backgrounds.   Before producing the final presentation, validate that:   1. The deck aligns with the provided learning objectives. 2. The content is suitable for a broad audience. 3. The modules and slides follow a logical progression. 4. Exercises and assessments reinforce the intended learning outcomes. 5. The presentation supports smooth facilitation and effective learner navigation. 6. The terminology, formatting, and content labels remain consistent. 7. The deck avoids unnecessary repetition and unexplained jargon. 8. The final output is professional, presentation-ready, and easy to understand.   Produce the final output as a complete presentation.`,
  },
  {
    header: 'Technical Content Deck',
    desc: 'Accurate, clear technical presentations',
    icon: '🛠️',
    color: 'pink',
    prompt: `Turn the provided technical documentation into a professional PowerPoint deck for both technical and non-technical users.

Simplify the content while preserving accuracy and essential details. Use a clear learning sequence, minimal text, one main idea per slide, and visual explanations such as diagrams, workflows, comparisons, and practical examples.

For each slide, include a title, key content, takeaway, visual or layout recommendation, and speaker notes when useful.

Use plain language, define technical terms, avoid unnecessary jargon, and place supporting details in the notes or appendix. Ensure the final deck is accurate, accessible, consistent, and presentation-ready.`,
  },
  {
    header: 'Teach-Back Learning Deck',
    desc: 'Active recall & learner participation',
    icon: '💬',
    color: 'cyan',
    prompt: `Act as an instructional designer and transform the provided training content into a professional, inclusive PowerPoint presentation for technical and non-technical learners.

Structure the deck to reinforce learning through simple concept summaries, practical examples, learner teach-back prompts, reflection activities, knowledge checks, key takeaways, and next steps.

For each slide, include:
- A clear title
- Minimal, presentation-ready content
- One key message
- A teach-back or discussion prompt
- A recommended visual or layout
- Speaker notes, where useful

Use plain language, define technical terms, and follow a logical learning sequence. Keep slides visually simple, consistent, and focused on active recall, learner participation, and practical application.

Ensure the final deck aligns with the learning objectives, preserves essential information, and is accessible to learners with different roles and experience levels.

Produce a complete slide-by-slide outline with content, prompts, visual guidance, and relevant speaker notes.`,
  },
  {
    header: 'Presentation Outline Deck',
    desc: 'Structure your ideas clearly',
    icon: '📊',
    color: 'green',
    prompt: `Act as an experienced instructional designer and technical trainer. Using the provided documents and curriculum topics, generate a structured presentation outline with modular objectives, key concepts, interactive discussions, and slide progression.`,
  },
  {
    header: 'Reference Design Deck',
    desc: 'Copy the design of your uploaded PPT',
    icon: '🎨',
    color: 'orange',
    prompt: `Copy the design of the uploaded reference PPT and build a new training deck with it.

Use the reference only for its design: its slide layouts, backgrounds, colours, fonts and title style. Do not reuse any of its text, numbers or pictures.

Topic of the new deck: [type your topic here]

Take the content from my other uploaded documents (or from the topic above if there are none). Keep each slide clear, with minimal text, and include speaker notes.`,
  },
];