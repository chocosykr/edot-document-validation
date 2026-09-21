# Document Validation Discovery Report
**Document**: `INDOS (20).pdf`
**Date**: 2026-09-12 12:10:21

## 1. Best Validation Sources
```json
{
  "verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping) - Indos Checker",
      "url": "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp",
      "source_type": "Official verification portal",
      "verification_capability": "provides_verification",
      "description": "An official tool provided by the Directorate General of Shipping (DG Shipping) to verify maritime certificates, including INDoS, CDC, and STCW courses. It allows users to search for certificate details using specific criteria such as Certificate Number and Date of Birth.",
      "is_authoritative": true,
      "is_official_government_site": true,
      "requires_login": false,
      "reliability_score": 1.0
    },
    {
      "source_name": "DG Shipping Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "verification_capability": "provides_verification",
      "description": "The official government portal for verifying various maritime certificates issued by the Directorate General of Shipping, including CDC, COC, COP, and Seafarer Identity Documents.",
      "is_authoritative": true,
      "is_official_government_site": true,
      "requires_login": false,
      "reliability_score": 1.0
    }
  ],
  "fallback_procedures": [
    {
      "procedure": "Access the official DG Shipping E-Governance portal to obtain a 'Master Checker printout' for official proof of the certificate.",
      "authority": "Directorate General of Shipping (DGS), Government of India"
    },
    {
      "procedure": "Manual verification via RPSL (Recruitment and Placement Services License) agencies or direct contact with the DGS Secretariat for official inquiries regarding maritime documentation.",
      "authority": "Directorate General of Shipping (DGS), Government of India"
    }
  ]
}
```

## 2. Search Queries Performed
- `official INDos certificate verification portal India`
- `Directorate General of Shipping India INDos verification`
- `how to verify Indian National Database of Seafarers certificate`
- `DG Shipping India INDos online verification system`
- `verify seafarer credentials India government portal`
- `Lal Bahadur Shastri College of Advanced Maritime Studies & Research certificate verification`
- `Indian maritime authority seafarer database verification procedures`
- `DG Shipping India INDos certificate authenticity check`

## 3. Analyzed Results Summary
### 3.1 Guidelines on INDoS Number and Seafarer Registration ...
- **URL**: https://www.facebook.com/DGShippingIndia/videos/guidelines-on-indos-number-and-seafarer-registration-directorate-general-of-ship/1259462188681756
- **Query**: `official INDos certificate verification portal India`
#### Analysis
```json
{
  "discovery_summary": {
    "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate",
    "verification_status": "verification_sources_identified",
    "total_sources_found": 1,
    "authoritative_sources_count": 1
  },
  "sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "url": "https://www.facebook.com/DGShippingIndia/videos/guidelines-on-indos-number-and-seafarer-registration-directorate-general-of-ship/1259462188681756",
      "source_type": "Official documentation describing verification procedures",
      "verification_capability": "Provides information about the verification procedure via the DGS e-governance portal.",
      "description": "The source indicates that the INDoS Certificate verification is performed via the DGS e-governance portal, specifically noting that a 'Master Checker printout' serves as official proof of the certificate.",
      "is_authoritative": true,
      "requires_login": true
    }
  ],
  "verification_pathways": [
    {
      "pathway_description": "Access the DGS e-governance portal to generate/obtain the Master Checker printout for official verification of the INDoS certificate.",
      "authority": "Directorate General of Shipping (DGS), Government of India"
    }
  ]
}
```

### 3.2 Indian National Database of Seafarer
- **URL**: http://220.156.189.33/esamudraUI/jsp/examination/checker/COCSearch.jsp?hidProcessId=COC
- **Query**: `official INDos certificate verification portal India`
#### Analysis
```json
{
  "discovery_summary": {
    "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate",
    "verification_status": "potential_verification_available",
    "authoritative_sources_found": 1,
    "total_sources_analyzed": 1
  },
  "sources": [
    {
      "source_name": "eSamudra (Directorate General of Shipping)",
      "url": "http://220.156.189.33/esamudraUI/jsp/examination/checker/COCSearch.jsp?hidProcessId=COC",
      "source_type": "Official verification portal",
      "verification_capability": "provides_verification",
      "description": "A portal for checking Certificates of Competency (CoC) within the Indian National Database of Seafarers (INDoS) system, used by statutory authorities to prevent fraudulent certificates.",
      "reliability_score": 1.0,
      "notes": "The source provides a direct link to a CoC search/checker function, which is the primary method for verifying maritime certificates in India."
    }
  ],
  "verification_procedure_details": {
    "procedure_description": "The INDoS system is a computerized national database used by Flag State, Port State, Immigration, and Employers to verify certificates and meet STCW regulatory requirements. The INDoS number is required for verification and can be found on the certificate (left side for booklet type, right side for paper type).",
    "required_information": [
      "INDoS Number"
    ]
  }
}
```

### 3.3 How to get DG Shipping INDoS Number - A Complete Guide
- **URL**: https://himtmarine.com/how-to-get-dg-shipping-indos-number-himt
- **Query**: `Directorate General of Shipping India INDos verification`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "source_url": null,
      "source_type": "Government/Maritime Authority",
      "verification_status": "Provides information about the existence and purpose of the INDoS registry",
      "description": "The INDoS is a centralized electronic registry maintained by the Directorate General of Maritime Administration (DGS) to store verified identity details of seafarers. It is used to prevent fraudulent claims and speed up verification for employers and licensing authorities.",
      "verification_method": "The registry is used for verifying identity, qualifications, and for CDC processing, though a direct public verification portal URL was not provided in the search results.",
      "reliability_score": 1.0,
      "notes": "The search result identifies the DGS as the authority managing the registry, but does not provide a direct link to a public verification portal."
    },
    {
      "source_name": "HIMT Marine (Training Institute)",
      "source_url": "https://himtmarine.com/how-to-get-dg-shipping-indos-number-himt",
      "source_type": "Information about the document",
      "verification_status": "Provides information about the document",
      "description": "A maritime training institute guide explaining the importance of the INDoS number and the process of application and verification through MTIs (Maritime Training Institutes).",
      "verification_method": null,
      "reliability_score": 0.5,
      "notes": "This is a third-party training provider providing guidance on the document, not an official verification authority."
    }
  ],
  "summary": {
    "verification_feasibility": "High (Official registry exists)",
    "verification_difficulty": "Medium (Requires official DGS channels)",
    "key_findings": "The INDoS is an official registry maintained by the Directorate General of Shipping (DGS) India. It is used to store verified seafarer details to prevent fraud. While the search results confirm the existence of the registry and its function for employers and authorities, a direct public verification URL was not provided in the search results."
  }
}
```

### 3.4 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `Directorate General of Shipping India INDos verification`
#### Analysis
```json
{
  "discovery_summary": {
    "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate",
    "verification_status": "possible",
    "authoritative_sources_found": 1,
    "total_sources_analyzed": 1
  },
  "sources": [
    {
      "source_name": "Directorate General of Shipping, Government of India",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "authority_level": "High",
      "verification_capability": "Provides a Seafarer Certificate Verification System for CDC and COC; INDos is the database underlying these credentials.",
      "description": "Official government portal for verifying maritime certificates issued by the Directorate General of Shipping.",
      "verification_details": "The portal explicitly offers verification for Continuous Discharge Certificates (CDC) and Certificates of Competency (COC). While the specific INDos number verification link is not explicitly detailed in the snippet, this is the primary authority for all Indian seafarer credentials.",
      "access_requirements": "Publicly accessible verification portal."
    }
  ],
  "source_classification": {
    "verification_provided": [
      "https://dgma.gov.in/seafarer-certificate-verification-system"
    ],
    "information_only": [],
    "procedure_explanation": [],
    "restricted_access": [],
    "unconfirmed_sources": []
  }
}
```

### 3.5 How to download INDOS certificate || Indian National database ...
- **URL**: https://www.youtube.com/watch?v=PPB1uEJEQv0
- **Query**: `how to verify Indian National Database of Seafarers certificate`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping), Government of India",
      "source_url": "https://dgshipping.gov.in",
      "source_type": "Official issuing organization",
      "verification_status": "Provides verification procedure",
      "description": "The official e-governance portal of the DG Shipping allows seafarers to log in to their profiles to view and print their INDos certificates. This portal is the authoritative source for managing and accessing official maritime credentials in India.",
      "verification_method": "Login to the DG Shipping e-governance portal using user credentials to access the seafarer profile and view/print the certificate.",
      "is_authoritative": true,
      "is_official_government_site": true,
      "requires_login": true
    },
    {
      "source_name": "DG Shipping E-Governance Portal",
      "source_url": null,
      "source_type": "Official verification portal",
      "verification_status": "Provides verification procedure",
      "description": "The specific section of the DG Shipping website used for seafarer profile management and certificate access.",
      "verification_method": "Access via the e-governance section of the DG Shipping website.",
      "is_authoritative": true,
      "is_official_government_site": true,
      "requires_login": true
    }
  ],
  "unverifiable_sources": []
}
```

### 3.6 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `how to verify Indian National Database of Seafarers certificate`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping) - Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "authority_level": "Government/Maritime Authority",
      "verification_capability": "Provides verification for various maritime documents including COP, Passport, CDC, and COC issued by the Directorate General of Shipping.",
      "verification_details": "The portal offers specific links to verify Certificates of Proficiency (COP), Seafarer Identity Documents, Continuous Discharge Certificates (CDC), and Certificates of Competency (COC). While the INDos number is a primary identifier for Indian seafarers, the portal serves as the official gateway for verifying the authenticity of certificates issued under the Indian maritime regulatory framework.",
      "reliability_score": 1.0,
      "notes": "This is the primary official government portal for maritime document verification in India."
    },
    {
      "source_name": "Directorate General of Shipping (DG Shipping) Official Website",
      "url": "https://dgma.gov.in/",
      "source_type": "Official issuing organization",
      "authority_level": "Government/Maritime Authority",
      "verification_capability": "Provides information about maritime regulations and official links to verification systems.",
      "verification_details": "The DG Shipping is the governing body for maritime affairs in India and the authority responsible for the INDos database and related certificates.",
      "reliability_score": 1.0,
      "notes": "The main authority for all Indian maritime documentation."
    }
  ],
  "summary": {
    "verification_status": "Verification possible via official government portals.",
    "primary_verification_method": "Use the DG Shipping Seafarer Certificate Verification System to verify maritime credentials issued by the Government of India."
  }
}
```

### 3.7 INDoS
- **URL**: https://dgma.gov.in/training-branch/training-branch-indos
- **Query**: `DG Shipping India INDos online verification system`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) E-Governance Portal",
      "url": "https://dgshipping.gov.in",
      "source_type": "Official verification portal",
      "authority_level": "Government/Flag-state authority",
      "verification_capability": "Provides an online e-governance system for seafarer modules, including INDoS registration and profile management.",
      "verification_method": "The document profile mentions a streamlined online procedure via the DG Shipping E-Governance website for managing seafarer profiles and data. While the specific 'verification' link for third parties is not explicitly detailed, the portal is the official authority for INDoS data.",
      "reliability_score": 1.0,
      "notes": "The search result points to the DG Shipping E-Governance website as the central hub for seafarer data management and profile updates."
    },
    {
      "source_name": "DGMA (Directorate General of Maritime Administration)",
      "url": "https://dgma.gov.in/training-branch/training-branch-indos",
      "source_type": "Official documentation describing verification procedures",
      "authority_level": "Government/Flag-state authority",
      "verification_capability": "Explains the procedure for INDoS registration and the role of Maritime Training Institutions (MTIs) in verifying documents.",
      "verification_method": "Describes the process where MTIs verify original documents (Passport/Mark sheets) and upload data to the DGS system.",
      "reliability_score": 0.9,
      "notes": "This source provides the procedural context for how INDoS data is generated and managed via the official government framework."
    }
  ],
  "summary": {
    "verification_status": "Possible via official government portals",
    "primary_authority": "Directorate General of Shipping (DGS), India",
    "verification_path": "The INDoS number is part of the official e-governance system managed by the DGS. Verification of seafarer details is typically conducted through the official DGS E-Governance portal used by maritime authorities and RPSL companies."
  }
}
```

### 3.8 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `DG Shipping India INDos online verification system`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official issuing organization",
      "verification_status": "Provides verification procedures",
      "description": "The official website of the Directorate General of Shipping (DGS) provides links for verifying maritime certificates, specifically mentioning Continuous Discharge Certificates (CDC) and Certificates of Competency (COC). While the INDos certificate itself is a database entry, this is the primary authority for maritime document verification in India.",
      "verification_method": "Online verification portal for maritime certificates",
      "is_authoritative": true,
      "requires_login": false
    },
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "url": "https://dgma.gov.in",
      "source_type": "Government website",
      "verification_status": "Provides information about the document",
      "description": "The main portal for the Directorate General of Shipping, the government authority responsible for issuing maritime certificates in India.",
      "verification_method": null,
      "is_authoritative": true,
      "requires_login": false
    }
  ]
}
```

### 3.9 The Directorate General of Shipping ...
- **URL**: https://www.instagram.com/reel/DNnghQSPXHD?hl=en
- **Query**: `verify seafarer credentials India government portal`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS), India",
      "source_url": "https://www.instagram.com/reel/DNnghQSPXHD?hl=en",
      "source_type": "Official documentation describing verification procedures",
      "is_authoritative": true,
      "verification_status": "Provides information about verification procedures",
      "description": "The Directorate General of Shipping (DGS) is the governing maritime authority in India. The search result indicates that verification of seafarer documents is a process managed through RPSL (Recruitment and Placement Services License) agencies, which submit documents to the DGS for verification to ensure industry integrity and compliance with international standards.",
      "verification_method": "Documents are submitted via RPSL agencies to the DGS for verification.",
      "access_requirements": "Not specified (implies agency-led process)"
    }
  ],
  "unverifiable_sources": []
}
```

### 3.10 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `verify seafarer credentials India government portal`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping) - Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "authority_level": "Government/Flag-state authority",
      "verification_capability": "Provides verification for CDC, COC, COP, Passport, and COE. While the document profile is an INDos Certificate, this portal is the official maritime authority for Indian seafarer credentials.",
      "reliability_assessment": "High - Official government maritime authority portal.",
      "notes": "The portal explicitly lists verification services for various maritime certificates issued by the Directorate General of Shipping."
    },
    {
      "source_name": "Directorate General of Shipping (DG Shipping)",
      "url": "https://dgma.gov.in/",
      "source_type": "Official issuing organization",
      "authority_level": "Government/Flag-state authority",
      "verification_capability": "Provides information regarding maritime credentials and official contact details for maritime administration.",
      "reliability_assessment": "High",
      "notes": "The primary authority for Indian maritime documentation."
    }
  ],
  "verification_procedure_sources": [
    {
      "source_name": "Directorate General of Shipping - Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "A source that explains a verification procedure",
      "authority_level": "Government/Flag-state authority",
      "verification_capability": "Explains the availability of online verification for various maritime certificates.",
      "reliability_assessment": "High",
      "notes": "Provides links to specific verification modules for different certificate types."
    }
  ]
}
```

### 3.11 Lal Bahadur Shastri College of Advanced Maritime Studies and Research | Shaalaa.com
- **URL**: https://www.shaalaa.com/lal-bahadur-shastri-college-of-advanced-maritime-studies-and-research
- **Query**: `Lal Bahadur Shastri College of Advanced Maritime Studies & Research certificate verification`
#### Analysis
```json
{
  "discovery_summary": {
    "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate",
    "verification_status": "uncertain",
    "authoritative_sources_found": 0,
    "total_sources_analyzed": 1
  },
  "sources": [
    {
      "source_name": "Shaalaa.com",
      "source_url": "https://www.shaalaa.com/lal-bahadur-shastri-college-of-advanced-maritime-studies-and-research",
      "source_type": "Information about the document/institution",
      "verification_capability": "none",
      "description": "Provides general information about the Lal Bahadur Shastri College of Advanced Maritime Studies & Research, noting it is a constituent institute of the Indian Maritime University.",
      "verification_details": null,
      "is_authoritative": false
    }
  ],
  "verification_pathways": {
    "official_verification_portals": null,
    "official_documentation_procedures": null,
    "official_issuing_organizations": [
      {
        "organization": "Indian Maritime University (IMU)",
        "description": "The college is a constituent institute of IMU-Mumbai Campus; official verification for maritime credentials in India typically flows through the Directorate General of Shipping (DGS) or the IMU portal."
      }
    ]
  }
}
```

### 3.12 Lal Bahadur Shastri College of Advanced Maritime Studies ...
- **URL**: https://easyshiksha.com/Lal-Bahadur-Shastri-College-of-Advanced-Maritime-Studies-and-Research-338842
- **Query**: `Lal Bahadur Shastri College of Advanced Maritime Studies & Research certificate verification`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "source_url": null,
      "source_type": "Official verification procedure",
      "description": "The document is an 'INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate'. INDos is a centralized database managed by the Government of India's Directorate General of Shipping. Verification of such maritime certificates typically requires access to the official DGS INDos portal.",
      "verification_status": "Provides information about the document type and its authority (Government of India), but does not provide a direct verification link in the search results.",
      "is_authoritative": true,
      "is_verification_portal": false,
      "is_procedure_description": true,
      "requires_login": true
    },
    {
      "source_name": "EasyShiksha",
      "source_url": "https://easyshiksha.com/Lal-Bahadur-Shastri-College-of-Advanced-Maritime-Studies-and-Research-338842",
      "source_type": "Information about the document",
      "description": "A third-party educational listing site providing information about the college. It does not provide official verification for government-issued maritime certificates.",
      "verification_status": "Does not provide verification.",
      "is_authoritative": false,
      "is_verification_portal": false,
      "is_procedure_description": false,
      "requires_login": false
    }
  ],
  "summary": {
    "verification_feasibility": "High (via official government channels)",
    "verification_difficulty": "Medium (requires official government portal access)",
    "notes": "The document is a government-issued maritime certificate (INDos). While the search results provide information about the issuing college, the actual verification must be performed through the official Indian Government (Directorate General of Shipping) maritime portals. The search results do not provide the specific URL for the INDos verification portal, but identify the authority as the Government of India."
  }
}
```

### 3.13 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `Indian maritime authority seafarer database verification procedures`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) - Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "authority_level": "Government/Maritime Authority",
      "verification_capability": "Provides verification for Certificates of Proficiency (COP), Seafarer Identity Documents, Passports, Certificates of Endorsement (COE), Continuous Discharge Certificates (CDC), and Certificates of Competency (COC).",
      "verification_status": "Provides actual verification",
      "notes": "The portal is the official government site for verifying various Indian maritime certificates issued by the Directorate General of Shipping."
    },
    {
      "source_name": "Directorate General of Shipping (DGS) Contact Information",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official documentation describing verification procedures",
      "authority_level": "Government/Maritime Authority",
      "verification_capability": "Provides contact details for the Director General of Shipping and DGS Secretariat for official inquiries.",
      "verification_status": "A source that explains a verification procedure",
      "notes": "Provides contact information for the DGS officials which can be used for manual verification or inquiries regarding maritime documentation."
    }
  ],
  "unverifiable_sources": []
}
```

### 3.14 DGS begins verification drive for seafarer certificates ...
- **URL**: https://www.maritimegateway.com/dgs-begins-verification-drive-for-seafarer-certificates-issued-overseas
- **Query**: `Indian maritime authority seafarer database verification procedures`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DGS) India",
      "source_url": null,
      "source_type": "Official issuing organization",
      "verification_status": "Provides information about verification procedures",
      "description": "The DGS is the primary maritime authority in India responsible for seafarer certification and verification drives to curb fraudulent credentials.",
      "evidence_provided": "The search results confirm that the DGS conducts verification drives for seafarer certificates to ensure authenticity and uphold international standards.",
      "reliability_score": 1.0
    },
    {
      "source_name": "Maritime Gateway",
      "source_url": "https://www.maritimegateway.com/dgs-begins-verification-drive-for-seafarer-certificates-issued-overseas",
      "source_type": "Maritime news/Information source",
      "verification_status": "Provides information about the document",
      "description": "A maritime industry news outlet reporting on DGS verification initiatives and regulatory directives.",
      "evidence_provided": "Provides context regarding the DGS's role in verifying certificates to prevent fraudulent credentials in the Indian maritime sector.",
      "reliability_score": 0.7
    }
  ],
  "verification_summary": {
    "verification_exists": true,
    "verification_method": "The Directorate General of Shipping (DGS) conducts verification drives and manages maritime credentials to ensure the authenticity of seafarer certificates.",
    "official_verification_portal": null,
    "notes": "While the search results confirm that the DGS is the authority responsible for certificate verification, a direct URL to an online verification portal for the INDos database was not provided in the search results."
  }
}
```

### 3.15 Seafarer Certificate Verification System
- **URL**: https://dgma.gov.in/seafarer-certificate-verification-system
- **Query**: `DG Shipping India INDos certificate authenticity check`
#### Analysis
```json
{
  "discovery_summary": {
    "document_type": "INDIAN NATIONAL DATABASE OF SEAFARERS (INDos) Certificate",
    "verification_status": "potential_verification_available",
    "authoritative_sources_found": 1,
    "total_sources_analyzed": 1
  },
  "sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping) - Seafarer Certificate Verification System",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "source_type": "Official verification portal",
      "authority_level": "High",
      "verification_capability": "Provides verification for CDC and COC; INDos verification capability is implied via the broader Seafarer Certificate Verification System context.",
      "description": "Official government portal for verifying maritime certificates issued by the Directorate General of Shipping.",
      "verification_status": "Provides verification for related maritime certificates (CDC/COC).",
      "notes": "While the content explicitly mentions CDC and COC verification, this is the official government portal for seafarer certificate verification in India."
    }
  ],
  "verification_pathways": [
    {
      "pathway_type": "Direct Online Verification",
      "description": "Use the DG Shipping Seafarer Certificate Verification System to check the authenticity of maritime credentials.",
      "url": "https://dgma.gov.in/seafarer-certificate-verification-system",
      "confidence_score": 0.8
    }
  ]
}
```

### 3.16 Indos Checker - Directorate General of Shipping
- **URL**: http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp
- **Query**: `DG Shipping India INDos certificate authenticity check`
#### Analysis
```json
{
  "document_verification_sources": [
    {
      "source_name": "Directorate General of Shipping (DG Shipping) - Indos Checker",
      "source_url": "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp",
      "source_type": "Official verification portal",
      "is_authoritative": true,
      "verification_status": "Provides actual verification",
      "verification_details": "The portal is an official tool provided by the Directorate General of Shipping (DG Shipping) to verify various maritime certificates, including INDoS, CDC, and STCW courses. It allows users to search for certificate details using specific criteria such as Certificate Number and Date of Birth.",
      "notes": "The URL appears to be a direct link to the eSamudra UI portal used by the Indian maritime authorities."
    }
  ]
}
```
